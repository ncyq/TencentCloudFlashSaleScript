"""离线验证：所有 HTTP 请求均由内存中的模拟客户端接管。"""
import threading
import time
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from unittest.mock import patch

import requests

import snap_up_server as app


class Response:
    def __init__(self, status=200, data=None, headers=None):
        self.status_code = status
        self.data = data
        self.headers = headers or {}
        self.closed = False

    def json(self):
        if isinstance(self.data, Exception):
            raise self.data
        return self.data

    def close(self):
        self.closed = True


class Harness:
    def __init__(self, behavior, prewarm_response=None):
        self.behavior = behavior
        self.prewarm_response = prewarm_response
        self.lock = threading.Lock()
        self.clients = []
        self.calls = []
        self.preflights = []
        self.counts = {}
        self.active = 0
        self.max_active = 0

    def factory(self, cookies):
        harness = self

        class Client:
            closed = False

            def post(self, url, **kwargs):
                payload = kwargs["json"]
                if url == app.CHECK_URL:
                    with harness.lock:
                        harness.preflights.append((time.monotonic(), payload, id(self)))
                    return harness.prewarm_response or Response(
                        data={"code": 0, "data": [{"available": 0}]})
                if url != app.BUY_URL:
                    raise AssertionError("Unexpected endpoint")
                region = payload["goods"][0]["goods_param"]["regionId"]
                with harness.lock:
                    attempt = harness.counts.get(region, 0) + 1
                    harness.counts[region] = attempt
                    harness.calls.append((region, time.monotonic(), kwargs, id(self)))
                    harness.active += 1
                    harness.max_active = max(harness.active, harness.max_active)
                try:
                    return harness.behavior(region, attempt)
                finally:
                    with harness.lock:
                        harness.active -= 1

            def close(self):
                self.closed = True

        client = Client()
        with self.lock:
            self.clients.append(client)
        return client


class RetryTests(unittest.TestCase):
    def setUp(self):
        self.logging = patch.object(app, "log")
        self.logging.start()
        self.addCleanup(self.logging.stop)
        self.settings = replace(app.Settings(), max_attempts=10, retry_base_ms=1, retry_max_ms=1,
                                parallel_requests_per_region=1, window_seconds=2)

    def run_workers(self, harness, regions=(1, 4, 8), **kwargs):
        return app.buy_now_concurrent(regions, [], settings=kwargs.pop("settings", self.settings),
                                     session_factory=harness.factory, warmup=kwargs.pop("warmup", False), **kwargs)

    def test_ten_attempts_per_region_and_three_in_flight(self):
        first_wave = threading.Barrier(3)

        def behavior(region, attempt):
            if attempt == 1:
                first_wave.wait(timeout=2)
            return Response(data={"code": -1, "msg": "库存不足"})

        harness = Harness(behavior)
        state = self.run_workers(harness)
        self.assertEqual(state.attempts, {1: 10, 4: 10, 8: 10})
        self.assertEqual(harness.counts, state.attempts)
        self.assertEqual(len(harness.calls), 30)
        self.assertEqual(harness.max_active, 3)
        self.assertIsNone(state.winner)
        self.assertTrue(all(client.closed for client in harness.clients))
        per_region_clients = {}
        for region, _, kwargs, client_id in harness.calls:
            per_region_clients.setdefault(region, set()).add(client_id)
            self.assertEqual(kwargs["timeout"], (3, 5))
            self.assertFalse(kwargs["allow_redirects"])
        self.assertEqual(len({next(iter(ids)) for ids in per_region_clients.values()}), 3)
        self.assertTrue(all(len(ids) == 1 for ids in per_region_clients.values()))

    def test_fast_success_stops_slow_regions_before_their_next_attempt(self):
        first_wave = threading.Barrier(3)
        success_published = threading.Event()
        publish = app.RunState.publish

        def observed_publish(state, region, outcome):
            publish(state, region, outcome)
            if outcome.kind == "success":
                success_published.set()

        def behavior(region, attempt):
            first_wave.wait(timeout=2)
            if region == 8:
                return Response(data={"code": 0, "data": {"orderId": "fake-order"}})
            if not success_published.wait(2):
                raise AssertionError("Fast success was not published immediately")
            return Response(data={"code": -1, "msg": "库存不足"})

        harness = Harness(behavior)
        with patch.object(app.RunState, "publish", observed_publish):
            state = self.run_workers(harness)
        self.assertEqual(state.winner[0], 8)
        self.assertEqual(harness.counts, {1: 1, 4: 1, 8: 1})
        self.assertTrue(state.stop.is_set())

    def test_connect_timeout_can_retry(self):
        def behavior(region, attempt):
            if attempt < 3:
                raise requests.ConnectTimeout()
            return Response(data={"code": 0})

        harness = Harness(behavior)
        state = self.run_workers(harness, regions=(1,))
        self.assertEqual(state.attempts[1], 3)
        self.assertIsNotNone(state.winner)

    def test_read_timeout_retries_within_limits(self):
        def behavior(region, attempt):
            raise requests.ReadTimeout()

        harness = Harness(behavior)
        state = self.run_workers(harness, regions=(1,))
        self.assertEqual(harness.counts[1], 10)
        self.assertFalse(state.stop.is_set())
        self.assertFalse(state.reasons)

    def test_unknown_business_error_retries_within_limits(self):
        harness = Harness(lambda *_: Response(data={"code": 12345, "msg": "未识别的错误"}))
        state = self.run_workers(harness, regions=(1,))
        self.assertEqual(harness.counts[1], 10)
        self.assertFalse(state.stop.is_set())
        self.assertFalse(state.reasons)

    def test_retry_after_does_not_pause_parallel_refill(self):
        def behavior(region, attempt):
            if attempt == 1:
                return Response(status=429, headers={"Retry-After": "0.06"})
            return Response(data={"code": 0})

        harness = Harness(behavior)
        state = self.run_workers(harness, regions=(1,))
        self.assertIsNotNone(state.winner)
        self.assertLess(harness.calls[1][1] - harness.calls[0][1], 0.05)

    def test_rate_limit_does_not_pause_other_regions(self):
        state = app.RunState([1, 4])
        before = time.monotonic()
        state.publish(1, app.Outcome("rate_limit", retry_after=0.04))
        self.assertTrue(state.admit(4, before + 1))
        self.assertLess(time.monotonic() - before, 0.02)

    def test_parallel_lanes_send_before_first_response_finishes(self):
        first_wave = threading.Barrier(3)

        def behavior(region, attempt):
            if attempt <= 3:
                first_wave.wait(timeout=1)
            return Response(data={"code": -1, "msg": "系统访问频率过快"})

        harness = Harness(behavior)
        settings = replace(self.settings, max_attempts=3,
                           parallel_requests_per_region=3, send_interval_ms=1,
                           window_seconds=0.2)
        state = self.run_workers(harness, regions=(1,), settings=settings)
        self.assertEqual(state.attempts[1], 3)
        self.assertEqual(harness.max_active, 3)
        self.assertIsNone(state.winner)

    def test_window_ends_during_retry_wait(self):
        harness = Harness(lambda *_: Response(data={"code": -1, "msg": "库存不足"}))
        settings = replace(self.settings, retry_base_ms=100, retry_max_ms=100, window_seconds=0.02)
        trigger = time.monotonic() + 0.01
        state = self.run_workers(harness, regions=(1,), settings=settings, trigger=trigger)
        self.assertGreaterEqual(state.attempts[1], 1)
        self.assertTrue(all(stamp < trigger + settings.window_seconds
                            for _, stamp, _, _ in harness.calls))

    def test_expired_window_never_submits(self):
        harness = Harness(lambda *_: Response(data={"code": 0}))
        state = self.run_workers(harness, trigger=time.monotonic() - 5)
        self.assertFalse(harness.calls)
        self.assertEqual(sum(state.attempts.values()), 0)

    def test_warmup_then_shared_start_uses_same_sessions(self):
        first_wave = threading.Barrier(3)

        def behavior(region, attempt):
            first_wave.wait(timeout=2)
            return Response(data={"code": 0})

        harness = Harness(behavior)
        target = time.monotonic() + 0.08
        self.run_workers(harness, trigger=target, warmup=True)
        self.assertEqual(len(harness.preflights), 3)
        self.assertEqual(len(harness.calls), 3)
        self.assertTrue(all(stamp >= target for _, stamp, _, _ in harness.calls))
        self.assertTrue(all(stamp < target for stamp, _, _ in harness.preflights))
        self.assertEqual({cid for _, _, cid in harness.preflights}, {cid for _, _, _, cid in harness.calls})

    def test_bad_request_during_prewarm_does_not_stop_purchase(self):
        harness = Harness(
            lambda *_: Response(data={"code": 0}),
            prewarm_response=Response(status=400, data={"code": -1, "msg": "参数错误"}),
        )
        state = self.run_workers(harness, regions=(1,), warmup=True)
        self.assertEqual(harness.counts, {1: 1})
        self.assertIsNotNone(state.winner)
        self.assertFalse(state.reasons)

    def test_classification_and_cleanup(self):
        samples = [
            (Response(status=403), "fatal"),
            (Response(status=503), "retry"),
            (Response(data=ValueError()), "retry"),
            (Response(data=[]), "retry"),
            (Response(data={"msg": "missing code"}), "retry"),
            (Response(data={"code": False}), "retry"),
            (Response(data={"code": -1, "msg": "CSRF token 已失效"}), "fatal"),
            (Response(data={"code": -1, "msg": "操作过于频繁"}), "rate_limit"),
            (Response(data={"code": -1, "msg": "活动尚未开始"}), "retry"),
        ]
        for response, kind in samples:
            with self.subTest(kind=kind, data=response.data):
                self.assertEqual(app.classify_response(response, self.settings).kind, kind)
        response = Response(data={"code": 0})
        client = unittest.mock.Mock()
        client.post.return_value = response
        self.assertEqual(app.buy_now(client, 1, self.settings).kind, "success")
        self.assertTrue(response.closed)

    def test_slow_prewarm_does_not_delay_other_regions_at_trigger(self):
        success_published = threading.Event()
        publish = app.RunState.publish

        def observed_publish(state, region, outcome):
            publish(state, region, outcome)
            if outcome.kind == "success":
                success_published.set()

        def prewarm(client, region, settings, state):
            if region == 1 and not success_published.wait(1):
                raise AssertionError("Other regions were blocked by slow prewarm")

        harness = Harness(lambda *_: Response(data={"code": 0}))
        with patch.object(app, "warm_connection", prewarm), patch.object(app.RunState, "publish", observed_publish):
            state = self.run_workers(harness, warmup=True, trigger=time.monotonic() + 0.04)
        self.assertIn(state.winner[0], (4, 8))
        self.assertEqual(state.attempts[1], 0)
        self.assertFalse(state.reasons)

    def test_invalid_configuration_and_retry_after(self):
        with self.assertRaises(ValueError):
            replace(self.settings, read_timeout_ms=0)
        with self.assertRaises(ValueError):
            replace(self.settings, retry_base_ms=500, retry_max_ms=300)
        self.assertEqual(app.retry_after_seconds("nan", 1), 1)
        self.assertEqual(app.retry_after_seconds("invalid", 1), 1)
        self.assertLess(app.Settings().read_timeout_ms / 1000, app.Settings().window_seconds)

    def test_crowded_http400_retries_until_success(self):
        harness = Harness(lambda region, attempt: Response(status=400, data={
            "code": -1, "msg": "同一时间下单人数过多"}) if attempt == 1 else Response(data={"code": "0"}))
        state = self.run_workers(harness, regions=(1,))
        self.assertEqual(state.attempts[1], 2)
        self.assertIsNotNone(state.winner)

    def test_http400_keeps_business_details(self):
        outcome = app.classify_response(Response(status=400, data={"code": 123, "msg": "参数错误"}), self.settings)
        self.assertEqual((outcome.kind, outcome.code, outcome.message), ("fatal", 123, "参数错误"))

    def test_http_error_code_zero_is_not_success(self):
        for status in (400, 500):
            self.assertNotEqual(app.classify_response(Response(status=status, data={"code": 0}), self.settings).kind, "success")

    def test_confirmed_ids_preserved(self):
        for region in (1, 4, 8):
            body = app.build_order_data(region)
            self.assertEqual(body["activity_id"], 164461404341040)
            self.assertEqual(body["goods"][0]["act_id"], 1897632168296710)
            self.assertEqual(body["goods"][0]["goods_param"]["regionId"], region)

    def test_next_rush_time(self):
        cases = [
            (datetime(2026, 9, 5, 9, 59, tzinfo=app.BEIJING), datetime(2026, 9, 5, 10, 0, tzinfo=app.BEIJING)),
            (datetime(2026, 9, 5, 10, 0, tzinfo=app.BEIJING), datetime(2026, 9, 5, 15, 0, tzinfo=app.BEIJING)),
            (datetime(2026, 9, 5, 14, 59, tzinfo=app.BEIJING), datetime(2026, 9, 5, 15, 0, tzinfo=app.BEIJING)),
            (datetime(2026, 9, 5, 15, 0, tzinfo=app.BEIJING), datetime(2026, 9, 6, 10, 0, tzinfo=app.BEIJING)),
            (datetime(2026, 9, 5, 20, 0, tzinfo=app.BEIJING), datetime(2026, 9, 6, 10, 0, tzinfo=app.BEIJING)),
            (datetime(2026, 9, 5, 1, 30, tzinfo=timezone.utc), datetime(2026, 9, 5, 10, 0, tzinfo=app.BEIJING)),
        ]
        for now, expected in cases:
            with self.subTest(now=now):
                self.assertEqual(app.next_rush_time(now), expected)
        with self.assertRaises(ValueError):
            app.next_rush_time(datetime(2026, 9, 5, 9, 0))


if __name__ == "__main__":
    unittest.main()
