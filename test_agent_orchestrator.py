import unittest

from agent_events import (
    FINAL_PARTIAL,
    FINAL_VERIFIED,
    FINISH_REQUESTED,
    INCREMENTAL_VERIFIED,
    MAX_STEPS_REACHED,
    PLAN_READY,
    REPLAN_REQUIRED,
    TARGET_FAILED,
    TOOL_FAILED,
    VERIFICATION_REGRESSED,
    VERIFICATION_UNVERIFIED,
    USER_CONFIRMED,
    AgentEvent,
)
from agent_orchestrator import ALLOWED_TRANSITIONS, AgentOrchestrator
from agent_state import DEBUGGING, DONE, EXECUTING, FAILED, PLANNING, VERIFYING, AgentState


class AgentOrchestratorTests(unittest.TestCase):
    def setUp(self):
        self.orchestrator = AgentOrchestrator(failed_finish_limit=2)

    def state(self, phase=PLANNING):
        return AgentState("state machine test", current_phase=phase)

    def test_finish_requires_verifying_before_done_and_records_history(self):
        state = self.state()
        self.orchestrator.transition(state, AgentEvent(PLAN_READY, "plan available"))
        self.assertEqual(state.current_phase, EXECUTING)

        self.orchestrator.transition(state, AgentEvent(FINISH_REQUESTED, "finish requested"))
        self.assertEqual(state.current_phase, VERIFYING)
        self.assertEqual(state.verification_mode, "FINAL")

        result = self.orchestrator.transition(
            state, AgentEvent(FINAL_VERIFIED, "all critical evidence passed", {"mode": "FINAL"})
        )
        self.assertEqual(result.next_phase, DONE)
        self.assertEqual(state.phase_history[-1]["event"], FINAL_VERIFIED)
        with self.assertRaises(ValueError):
            self.orchestrator.transition(state, AgentEvent(MAX_STEPS_REACHED, "too late"))

    def test_incremental_verified_returns_to_execution(self):
        state = self.state(VERIFYING)
        state.verification_mode = "INCREMENTAL"
        result = self.orchestrator.transition(state, AgentEvent(INCREMENTAL_VERIFIED, "step evidence passed"))
        self.assertEqual(result.next_phase, EXECUTING)

    def test_regressed_target_failed_and_unverified_cannot_done(self):
        regressed = self.state(VERIFYING)
        transition = self.orchestrator.transition(
            regressed, AgentEvent(VERIFICATION_REGRESSED, "new regression", {"mode": "FINAL"})
        )
        self.assertEqual(transition.next_phase, DEBUGGING)

        target = self.state(VERIFYING)
        transition = self.orchestrator.transition(
            target, AgentEvent(TARGET_FAILED, "critical target failed", {"mode": "FINAL"})
        )
        self.assertEqual(transition.next_phase, DEBUGGING)

        unverified = self.state(VERIFYING)
        transition = self.orchestrator.transition(
            unverified, AgentEvent(VERIFICATION_UNVERIFIED, "critical evidence unavailable", {"mode": "FINAL"})
        )
        self.assertEqual(transition.next_phase, VERIFYING)
        self.assertTrue(transition.pause_autonomous_loop)
        self.assertTrue(unverified.needs_user_confirmation)
        resumed = self.orchestrator.transition(unverified, AgentEvent(USER_CONFIRMED, "user supplied confirmation"))
        self.assertFalse(resumed.needs_user_confirmation)
        self.assertEqual(resumed.next_phase, VERIFYING)

    def test_repeated_failure_replans_and_final_partial_can_done(self):
        state = self.state(EXECUTING)
        self.orchestrator.transition(state, AgentEvent(TOOL_FAILED, "test failed"))
        self.assertEqual(state.current_phase, DEBUGGING)
        self.orchestrator.transition(state, AgentEvent(REPLAN_REQUIRED, "repeated failure fingerprint"))
        self.assertEqual(state.current_phase, PLANNING)
        self.assertEqual(state.replan_reason, "repeated failure fingerprint")

        partial = self.state(VERIFYING)
        partial.manual_confirmation_items = ["AC_DOC"]
        self.orchestrator.transition(partial, AgentEvent(FINAL_PARTIAL, "only non-critical human item remains"))
        self.assertEqual(partial.current_phase, DONE)
        self.assertEqual(partial.manual_confirmation_items, ["AC_DOC"])

    def test_illegal_transitions_and_global_max_steps(self):
        with self.assertRaises(ValueError):
            self.orchestrator.transition(self.state(EXECUTING), AgentEvent(FINAL_VERIFIED, "cannot skip verifying"))
        with self.assertRaises(ValueError):
            self.orchestrator.transition(self.state(VERIFYING), AgentEvent(PLAN_READY, "illegal"))

        for phase in (PLANNING, EXECUTING, VERIFYING, DEBUGGING):
            state = self.state(phase)
            result = self.orchestrator.transition(state, AgentEvent(MAX_STEPS_REACHED, "guardrail"))
            self.assertEqual(result.next_phase, FAILED)

        for (source, _event), target in ALLOWED_TRANSITIONS.items():
            self.assertIn(source, {PLANNING, EXECUTING, VERIFYING, DEBUGGING})
            self.assertIn(target, {PLANNING, EXECUTING, VERIFYING, DEBUGGING, DONE})


if __name__ == "__main__":
    unittest.main()
