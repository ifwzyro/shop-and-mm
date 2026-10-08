"""Database-level tests for MM escrow logic.

Run:  python -m tests.test_database
Covers: duplicate tickets, cooldowns, role pair validation, confirmations,
atomic MM claiming (race safety), release/complete/cancel guards, report
duplicate protection and restart survival (spec tests 2, 6-13, 19-21).
"""

from __future__ import annotations

import asyncio
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import config
from database import (
    CooldownError,
    Database,
    TicketExistsError,
    TicketStatus,
)
from utils.checks import validate_role_pair

GUILD = 111222333
CREATOR = 100000000000000001
PARTNER = 100000000000000002
MM1 = 200000000000000001
MM2 = 200000000000000002


class DatabaseTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "mm.db"
        self.db = Database(self.db_path)
        await self.db.initialize()
        self._t = 1_700_000_000

    async def asyncTearDown(self) -> None:
        await self.db.close()
        self.tmp.cleanup()

    def tick(self, seconds: int = 1) -> int:
        self._t += seconds
        return self._t

    # -- helpers --------------------------------------------------------

    async def new_ticket(self):
        ticket = await self.db.create_ticket(GUILD, CREATOR, self.tick())
        await self.db.set_channel(ticket.id, 500000 + ticket.ticket_number, self.tick())
        await self.db.set_status(
            ticket.id, TicketStatus.WAITING_FOR_PARTNER, now=self.tick(), expect=TicketStatus.CREATED
        )
        return await self.db.get_ticket(ticket.id)

    async def ticket_with_partner(self):
        ticket = await self.new_ticket()
        assert await self.db.add_partner(ticket.id, PARTNER, self.tick())
        await self.db.set_status(
            ticket.id,
            TicketStatus.WAITING_FOR_ROLES,
            now=self.tick(),
            expect=TicketStatus.PARTNER_ADDED,
        )
        return await self.db.get_ticket(ticket.id)

    async def ticket_ready_for_confirmation(self):
        ticket = await self.ticket_with_partner()
        await self.db.set_trader_role(ticket.id, CREATOR, "SELLER", self.tick())
        await self.db.set_trader_role(ticket.id, PARTNER, "BUYER", self.tick())
        started = await self.db.start_confirmation(
            ticket.id, self.tick(), self._t + config.CONFIRMATION_TIMEOUT, False
        )
        self.assertTrue(started)
        return await self.db.get_ticket(ticket.id)

    async def ticket_waiting_for_mm(self):
        ticket = await self.ticket_ready_for_confirmation()
        self.assertEqual(await self.db.confirm_trader(ticket.id, CREATOR, self.tick(), False), "ok_partial")
        self.assertEqual(await self.db.confirm_trader(ticket.id, PARTNER, self.tick(), False), "ok_both")
        self.assertTrue(await self.db.begin_mm_request(ticket.id, self.tick()))
        return await self.db.get_ticket(ticket.id)

    # -- spec test 2: duplicate tickets ---------------------------------

    async def test_duplicate_active_ticket_blocked(self):
        await self.new_ticket()
        with self.assertRaises(TicketExistsError):
            await self.db.create_ticket(GUILD, CREATOR, self.tick())

    async def test_partner_membership_blocks_new_ticket(self):
        await self.ticket_with_partner()
        with self.assertRaises(TicketExistsError):
            await self.db.create_ticket(GUILD, PARTNER, self.tick())

    async def test_cooldown_blocks_rapid_creation(self):
        original = config.TICKET_COOLDOWN
        config.TICKET_COOLDOWN = 10_000
        self.addCleanup(setattr, config, "TICKET_COOLDOWN", original)
        # First ticket then close it — cooldown still applies to new creations.
        ticket = await self.new_ticket()
        await self.db.close_ticket(ticket.id, CREATOR, self.tick(), "test")
        with self.assertRaises(CooldownError):
            await self.db.create_ticket(GUILD, CREATOR, self.tick())

    # -- spec tests 6/7: role pair validation ---------------------------

    def test_both_buyer_rejected(self):
        state, message = validate_role_pair(["BUYER", "BUYER"], allow_same=False)
        self.assertEqual(state, "conflict")
        self.assertIn("Both traders cannot be Buyer", message)
        self.assertIn("Seller", message)

    def test_both_seller_rejected(self):
        state, message = validate_role_pair(["SELLER", "SELLER"], allow_same=False)
        self.assertEqual(state, "conflict")
        self.assertIn("Both traders cannot be Seller", message)
        self.assertIn("Buyer", message)

    def test_mixed_pair_valid(self):
        self.assertEqual(validate_role_pair(["BUYER", "SELLER"], False)[0], "valid")
        self.assertEqual(validate_role_pair(["SELLER", "BUYER"], False)[0], "valid")

    def test_same_pair_allowed_when_configured(self):
        self.assertEqual(validate_role_pair(["BUYER", "BUYER"], True)[0], "valid")

    def test_incomplete_pair(self):
        self.assertEqual(validate_role_pair(["BUYER", None], False)[0], "incomplete")

    async def test_role_change_resets_confirmations(self):
        ticket = await self.ticket_ready_for_confirmation()
        await self.db.confirm_trader(ticket.id, CREATOR, self.tick(), False)
        trader = await self.db.get_trader(ticket.id, CREATOR)
        self.assertTrue(trader.confirmed)

        await self.db.set_trader_role(ticket.id, PARTNER, "SELLER", self.tick())
        traders = await self.db.get_traders(ticket.id)
        self.assertFalse(any(t.confirmed for t in traders), "role change must reset confirmations")

    # -- spec tests 8-11: confirmation ----------------------------------

    async def test_confirmation_flow_both_confirm(self):
        ticket = await self.ticket_ready_for_confirmation()
        self.assertEqual(
            await self.db.confirm_trader(ticket.id, CREATOR, self.tick(), False), "ok_partial"
        )
        self.assertEqual(
            await self.db.confirm_trader(ticket.id, CREATOR, self.tick(), False), "already"
        )
        self.assertEqual(
            await self.db.confirm_trader(ticket.id, PARTNER, self.tick(), False), "ok_both"
        )
        current = await self.db.get_ticket(ticket.id)
        self.assertEqual(current.status, TicketStatus.CONFIRMED)
        self.assertTrue(await self.db.begin_mm_request(ticket.id, self.tick()))
        current = await self.db.get_ticket(ticket.id)
        self.assertEqual(current.status, TicketStatus.WAITING_FOR_MM)
        self.assertIsNotNone(current.mm_requested_at)

    async def test_non_trader_cannot_confirm(self):
        ticket = await self.ticket_ready_for_confirmation()
        self.assertEqual(
            await self.db.confirm_trader(ticket.id, MM1, self.tick(), False), "not_trader"
        )

    async def test_decline_resets_confirmation(self):
        ticket = await self.ticket_ready_for_confirmation()
        await self.db.confirm_trader(ticket.id, CREATOR, self.tick(), False)
        self.assertTrue(
            await self.db.reset_confirmation(
                ticket.id, self.tick(), event_type="TRADER_DECLINED",
                actor_id=CREATOR, metadata={"reason": "changed my mind"},
            )
        )
        current = await self.db.get_ticket(ticket.id)
        self.assertEqual(current.status, TicketStatus.ROLES_SELECTED)
        traders = await self.db.get_traders(ticket.id)
        self.assertFalse(any(t.confirmed for t in traders))
        events = await self.db.get_events(ticket.id)
        self.assertIn("TRADER_DECLINED", [e.event_type for e in events])
        # Second reset must fail (guarded).
        self.assertFalse(
            await self.db.reset_confirmation(
                ticket.id, self.tick(), event_type="TRADER_DECLINED", actor_id=CREATOR
            )
        )

    # -- spec test 13: atomic claiming ----------------------------------

    async def test_exactly_one_mm_wins_claim_race(self):
        ticket = await self.ticket_waiting_for_mm()
        results = await asyncio.gather(
            self.db.claim_ticket(ticket.id, MM1, self.tick()),
            self.db.claim_ticket(ticket.id, MM2, self.tick()),
        )
        winners = [r for r in results if r.ok]
        losers = [r for r in results if not r.ok]
        self.assertEqual(len(winners), 1, "exactly one MM may win the claim")
        self.assertEqual(len(losers), 1)
        current = await self.db.get_ticket(ticket.id)
        self.assertEqual(losers[0].claimed_by, current.claimed_mm_id)
        self.assertEqual(current.status, TicketStatus.MM_CLAIMED)
        self.assertIn(current.claimed_mm_id, (MM1, MM2))
        claims = await self.db.get_claims(ticket.id)
        self.assertEqual(len(claims), 1)

    async def test_claim_blocked_after_first_claim(self):
        ticket = await self.ticket_waiting_for_mm()
        first = await self.db.claim_ticket(ticket.id, MM1, self.tick())
        self.assertTrue(first.ok)
        second = await self.db.claim_ticket(ticket.id, MM2, self.tick())
        self.assertFalse(second.ok)
        self.assertEqual(second.claimed_by, MM1)

    async def test_force_claim_blocked_when_claimed(self):
        ticket = await self.ticket_waiting_for_mm()
        self.assertTrue((await self.db.claim_ticket(ticket.id, MM1, self.tick())).ok)
        forced = await self.db.force_claim_ticket(ticket.id, MM2, MM2, self.tick())
        self.assertFalse(forced.ok)
        self.assertEqual(forced.claimed_by, MM1)

    # -- spec test 14: release ------------------------------------------

    async def test_release_returns_to_waiting_state(self):
        ticket = await self.ticket_waiting_for_mm()
        self.assertTrue((await self.db.claim_ticket(ticket.id, MM1, self.tick())).ok)
        self.assertTrue(await self.db.release_ticket(ticket.id, MM1, self.tick(), "test"))
        current = await self.db.get_ticket(ticket.id)
        self.assertEqual(current.status, TicketStatus.WAITING_FOR_MM)
        self.assertIsNone(current.claimed_mm_id)
        claims = await self.db.get_claims(ticket.id)
        self.assertIsNotNone(claims[0].released_at)
        # Re-claim works and appends history.
        self.assertTrue((await self.db.claim_ticket(ticket.id, MM2, self.tick())).ok)
        self.assertEqual(len(await self.db.get_claims(ticket.id)), 2)
        self.assertTrue(await self.db.release_ticket(ticket.id, MM2, self.tick(), "second release"))
        # Nothing claimed now — release is a no-op (state guard).
        self.assertFalse(await self.db.release_ticket(ticket.id, MM2, self.tick()))

    async def test_complete_requires_claim(self):
        ticket = await self.ticket_waiting_for_mm()
        self.assertFalse(await self.db.complete_ticket(ticket.id, MM1, self.tick()))
        await self.db.claim_ticket(ticket.id, MM1, self.tick())
        self.assertTrue(await self.db.complete_ticket(ticket.id, MM1, self.tick()))
        current = await self.db.get_ticket(ticket.id)
        self.assertEqual(current.status, TicketStatus.COMPLETED)
        self.assertIsNotNone(current.closed_at)
        # Terminal tickets cannot be cancelled again.
        self.assertFalse(await self.db.cancel_ticket(ticket.id, CREATOR, self.tick(), None))

    async def test_cancel_and_close_guards(self):
        ticket = await self.ticket_with_partner()
        self.assertTrue(await self.db.cancel_ticket(ticket.id, CREATOR, self.tick(), "no longer needed"))
        current = await self.db.get_ticket(ticket.id)
        self.assertEqual(current.status, TicketStatus.CANCELLED)
        self.assertEqual(current.cancel_reason, "no longer needed")
        self.assertFalse(await self.db.cancel_ticket(ticket.id, CREATOR, self.tick(), None))
        self.assertTrue(await self.db.close_ticket(ticket.id, MM1, self.tick(), "cleanup"))
        current = await self.db.get_ticket(ticket.id)
        self.assertEqual(current.status, TicketStatus.CLOSED)

    # -- spec test 19: report duplicate protection -----------------------

    async def test_report_duplicate_blocked(self):
        ticket = await self.ticket_waiting_for_mm()
        await self.db.claim_ticket(ticket.id, MM1, self.tick())
        report, outcome = await self.db.create_report(
            ticket_id=ticket.id, reporter_id=CREATOR, reported_mm_id=MM1,
            category="Scam / Fraud", description="They took my items and left.",
            prev_ticket_status=TicketStatus.MM_CLAIMED, now=self.tick(),
        )
        self.assertEqual(outcome, "created")
        self.assertIsNotNone(report)
        again, outcome2 = await self.db.create_report(
            ticket_id=ticket.id, reporter_id=CREATOR, reported_mm_id=MM1,
            category="Other", description="Second attempt should be blocked.",
            prev_ticket_status=TicketStatus.MM_CLAIMED, now=self.tick(),
        )
        self.assertEqual(outcome2, "duplicate")
        self.assertIsNone(again)

    async def test_report_multiple_allowed_when_configured(self):
        original = config.ALLOW_MULTIPLE_REPORTS
        config.ALLOW_MULTIPLE_REPORTS = True
        self.addCleanup(setattr, config, "ALLOW_MULTIPLE_REPORTS", original)
        ticket = await self.ticket_waiting_for_mm()
        await self.db.claim_ticket(ticket.id, MM1, self.tick())
        for _ in range(2):
            _, outcome = await self.db.create_report(
                ticket_id=ticket.id, reporter_id=CREATOR, reported_mm_id=MM1,
                category="Other", description="Repeated reports allowed by config.",
                prev_ticket_status=TicketStatus.MM_CLAIMED, now=self.tick(),
            )
            self.assertEqual(outcome, "created")

    # -- spec tests 20/21: restart survival ------------------------------

    async def test_state_survives_restart(self):
        ticket = await self.ticket_waiting_for_mm()
        await self.db.claim_ticket(ticket.id, MM1, self.tick())
        await self.db.create_report(
            ticket_id=ticket.id, reporter_id=PARTNER, reported_mm_id=MM1,
            category="Harassment", description="Rude behaviour during the trade.",
            prev_ticket_status=TicketStatus.MM_CLAIMED, now=self.tick(),
        )
        await self.db.set_message_id(ticket.id, "status_message_id", 9001)
        await self.db.set_message_id(ticket.id, "mm_message_id", 9002)

        # Simulate restart: close the connection, reopen the file.
        await self.db.close()
        reopened = Database(self.db_path)
        await reopened.initialize()
        try:
            restored = await reopened.get_ticket(ticket.id)
            self.assertIsNotNone(restored)
            self.assertEqual(restored.status, TicketStatus.MM_CLAIMED)
            self.assertEqual(restored.claimed_mm_id, MM1)
            self.assertEqual(restored.channel_id, ticket.channel_id)
            self.assertEqual(restored.mm_message_id, 9002)

            traders = await reopened.get_traders(ticket.id)
            roles = {t.user_id: t.trade_role for t in traders}
            self.assertEqual(roles[CREATOR], "SELLER")
            self.assertEqual(roles[PARTNER], "BUYER")

            claims = await reopened.get_claims(ticket.id)
            self.assertEqual(len(claims), 1)
            self.assertEqual(claims[0].mm_id, MM1)

            has_report = await reopened.has_report(ticket.id, PARTNER)
            self.assertTrue(has_report)

            events = await reopened.get_events(ticket.id)
            types = [e.event_type for e in events]
            for expected in (
                "TICKET_CREATED", "PARTNER_ADDED", "ROLE_SELECTED",
                "CONFIRMATION_STARTED", "TRADER_CONFIRMED", "MM_REQUESTED", "MM_CLAIMED",
            ):
                self.assertIn(expected, types)
        finally:
            await reopened.close()

    # -- monitor helpers -------------------------------------------------

    async def test_warning_and_flag_fire_once(self):
        ticket = await self.new_ticket()
        self.assertTrue(await self.db.mark_warned(ticket.id, "partner", self.tick()))
        self.assertFalse(await self.db.mark_warned(ticket.id, "partner", self.tick()))
        self.assertTrue(await self.db.mark_warned(ticket.id, "roles", self.tick()))

        self.assertTrue(await self.db.flag_ticket(ticket.id, self.tick()))
        self.assertFalse(await self.db.flag_ticket(ticket.id, self.tick()))
        current = await self.db.get_ticket(ticket.id)
        self.assertEqual(current.flagged, 1)

    async def test_terminal_tickets_not_listed_active(self):
        ticket = await self.new_ticket()
        await self.db.close_ticket(ticket.id, CREATOR, self.tick(), "done")
        active = await self.db.list_active_tickets()
        self.assertNotIn(ticket.id, [t.id for t in active])
        found = await self.db.find_active_ticket_for_user(CREATOR)
        self.assertIsNone(found)

    async def test_channel_binding_lookup(self):
        ticket = await self.new_ticket()
        by_channel = await self.db.get_ticket_by_channel(ticket.channel_id)
        self.assertIsNotNone(by_channel)
        self.assertEqual(by_channel.id, ticket.id)
        by_number = await self.db.get_ticket_by_number(GUILD, ticket.ticket_number)
        self.assertEqual(by_number.id, ticket.id)


if __name__ == "__main__":
    unittest.main(verbosity=2)
