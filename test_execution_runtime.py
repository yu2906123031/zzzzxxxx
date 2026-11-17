import copy,unittest
from execution_runtime import confirmation
from test_execution import config,books,signal

class ConfirmationTests(unittest.TestCase):
    def test_fast_confirmation_independent_of_full_scan(self):
        c=config();s=signal();s['baseline']=0;s['cost_bps']=22
        previous=None
        for t in range(1000,1031,2):previous=confirmation(s,books(t),t,c,previous)
        self.assertEqual(previous['duration'],30)
    def test_quote_gap_resets_confirmation(self):
        c=config();s=signal();a=confirmation(s,books(1000),1000,c)
        self.assertEqual(confirmation(s,books(1020),1020,c,a)['duration'],0)
    def test_vanished_edge_and_stale_book_reject(self):
        c=config();s=signal()
        self.assertIsNone(confirmation(s,books(1000,b=(99,100)),1000,c))
        self.assertIsNone(confirmation(s,books(900),1000,c))

if __name__=='__main__':unittest.main()
