from __future__ import annotations

import unittest

from rag_ime.agent_skill_routing import (
    default_skill_routing,
    scenario_for_session,
    skill_allowlist_for_session,
)


class AgentSkillRoutingTests(unittest.TestCase):
    def test_durable_surface_and_room_membership_select_the_scenario(self) -> None:
        self.assertEqual(
            scenario_for_session(
                {
                    "surfaceKind": "extension_app",
                    "ownerAppId": "extension:trace-agent",
                    "surfaceKey": "diagnostic",
                },
                room_participant=False,
            ),
            "trace",
        )
        self.assertEqual(
            scenario_for_session(
                {
                    "surfaceKind": "extension_app",
                    "ownerAppId": "extension:agent-lab",
                    "surfaceKey": "experiment.enterpriseops",
                },
                room_participant=True,
            ),
            "agentLab",
        )
        self.assertEqual(
            scenario_for_session(
                {
                    "surfaceKind": "agent",
                    "ownerAppId": "extension:trace-agent",
                    "surfaceKey": "diagnostic",
                },
                room_participant=False,
            ),
            "ordinary",
        )
        self.assertEqual(
            scenario_for_session(
                {
                    "surfaceKind": "extension_app",
                    "ownerAppId": "extension:agent-lab",
                    "surfaceKey": "unowned",
                },
                room_participant=False,
            ),
            "ordinary",
        )
        self.assertEqual(
            scenario_for_session({"surfaceKind": "agent"}, room_participant=True),
            "room",
        )
        self.assertEqual(
            scenario_for_session({"surfaceKind": "agent"}, room_participant=False),
            "ordinary",
        )

    def test_effective_allowlist_never_leaks_private_skills_across_scenarios(self) -> None:
        configuration = {"skillRouting": default_skill_routing()}
        cases = (
            (
                {"surfaceKind": "agent"},
                False,
                {"systematic-debugging"},
                {
                    "facilitate-room",
                    "trace-agent-diagnostics",
                    "agent-eval-room-optimizer",
                },
            ),
            (
                {"surfaceKind": "agent"},
                True,
                {"facilitate-room"},
                {
                    "trace-agent-diagnostics",
                    "agent-eval-room-optimizer",
                },
            ),
            (
                {
                    "surfaceKind": "extension_app",
                    "ownerAppId": "extension:trace-agent",
                    "surfaceKey": "repair",
                },
                False,
                {"trace-agent-diagnostics"},
                {
                    "facilitate-room",
                    "agent-eval-room-optimizer",
                },
            ),
            (
                {
                    "surfaceKind": "extension_app",
                    "ownerAppId": "extension:agent-lab",
                    "surfaceKey": "wizard",
                },
                True,
                {"agent-eval-room-optimizer", "facilitate-room"},
                {"trace-agent-diagnostics"},
            ),
        )
        for session, room_participant, expected, forbidden in cases:
            with self.subTest(session=session, room_participant=room_participant):
                selected = set(
                    skill_allowlist_for_session(
                        configuration,
                        session,
                        room_participant=room_participant,
                    )
                )
                self.assertTrue(expected.issubset(selected))
                self.assertTrue(selected.isdisjoint(forbidden))

    def test_verified_extension_app_skill_is_available_only_to_its_owner(self) -> None:
        configuration = {"skillRouting": default_skill_routing()}
        owners = {"zhanggui-wenshu": "extension:zhanggui-wenshu"}
        for session, expected in (
            ({"surfaceKind": "extension_app", "ownerAppId": "extension:zhanggui-wenshu"}, True),
            ({"surfaceKind": "extension_app", "ownerAppId": "extension:other-app"}, False),
            ({"surfaceKind": "agent", "ownerAppId": "extension:zhanggui-wenshu"}, False),
        ):
            with self.subTest(session=session):
                selected = skill_allowlist_for_session(
                    configuration,
                    session,
                    room_participant=False,
                    extension_app_skill_owners=owners,
                )
                self.assertEqual("zhanggui-wenshu" in selected, expected)
