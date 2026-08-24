from pathlib import Path
import tempfile
import unittest

from database import create_database, create_session_factory
from services import ActionAlreadyRunning, ActionGuard


class ActionGuardTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.guard=ActionGuard(create_session_factory(create_database(Path(self.tmp.name)/"guard.db")))

    def tearDown(self):
        self.tmp.cleanup()

    def test_identical_running_action_is_rejected_server_side(self):
        run_id=self.guard.start("stock:1:market")
        with self.assertRaises(ActionAlreadyRunning):
            self.guard.start("stock:1:market")
        self.guard.finish(run_id,True)
        next_id=self.guard.start("stock:1:market")
        self.assertNotEqual(run_id,next_id)


if __name__=="__main__":unittest.main()
