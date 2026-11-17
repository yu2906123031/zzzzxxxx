import unittest
from feeds import local_snapshot_time

class ClockTests(unittest.TestCase):
    def test_server_clock_offset_is_removed(self):
        raw={'_http_clock':dict(server_time=2000,started=1000,cache_age=0)}
        self.assertEqual(local_snapshot_time(2000,raw)[0],999)
    def test_old_snapshot_stays_old(self):
        raw={'_http_clock':dict(server_time=2000,started=1000,cache_age=0)}
        self.assertEqual(local_snapshot_time(1900,raw)[0],899)
    def test_cached_response_stays_old(self):
        raw={'_http_clock':dict(server_time=2000,started=1000,cache_age=20)}
        self.assertEqual(local_snapshot_time(2000,raw)[0],979)
    def test_future_snapshot_is_rejected(self):
        raw={'_http_clock':dict(server_time=2000,started=1000,cache_age=0)}
        with self.assertRaises(ValueError):local_snapshot_time(2003,raw)
    def test_no_date_keeps_original_clock(self):
        self.assertEqual(local_snapshot_time(2000,{})[0],2000)

if __name__=='__main__':unittest.main()
