import asyncio
import builtins
import os
import time
import unittest
from unittest.mock import AsyncMock, Mock, patch

_open = builtins.open


def _open_without_debug_log(file, *args, **kwargs):
    if isinstance(file, (str, os.PathLike)) and os.path.basename(file) == "tsum_debug.log":
        file = os.devnull
    return _open(file, *args, **kwargs)


with patch("builtins.open", side_effect=_open_without_debug_log):
    import tsum_login
    with patch.object(tsum_login, "resolve_appver"):
        import tsum_bot


class OwnedTsumLevelTests(unittest.TestCase):
    def setUp(self):
        debug = patch.object(tsum_bot, "_dbg")
        debug.start()
        self.addCleanup(debug.stop)

    def forge(self, rows):
        forge = Mock()
        forge.last_http_status = None
        forge.last_error = None
        forge.get_info.return_value = {"retcode": 0, "tsuminfo": rows}
        forge.max_tsum_level.return_value = {"status": "done"}
        return forge

    def run_all(self, forge):
        return tsum_bot.forge_sync(tsum_all_lv=1, sess={"userid": "test"}, f=forge)

    def test_only_owned_non_max_tsums_are_processed(self):
        forge = self.forge([
            {"tsumid": 1, "getflg": 1, "lv": 10},
            {"tsumid": 2, "getflg": 1, "lv": 50},
            {"tsumid": 3, "getflg": 0, "lv": 0},
            {"tsumid": 4, "setflg": 1, "lv": 5},
        ])
        result, _ = self.run_all(forge)
        self.assertEqual(result["count"], 3)
        self.assertEqual([call.args[0] for call in forge.max_tsum_level.call_args_list], [1, 4])

    def test_failure_reports_partial_progress_and_stops(self):
        forge = self.forge([
            {"tsumid": 1, "getflg": 1, "lv": 10},
            {"tsumid": 2, "getflg": 1, "lv": 10},
            {"tsumid": 3, "getflg": 1, "lv": 10},
        ])
        forge.max_tsum_level.side_effect = [
            {"status": "done"},
            {"status": "insufficient_heart", "have": 2, "need": 10},
        ]
        result, text = self.run_all(forge)
        self.assertIsNone(result)
        self.assertIn("完了 1/3", text)
        self.assertIn("ハート", text)
        self.assertEqual(forge.max_tsum_level.call_count, 2)

        forge.get_info.return_value["tsuminfo"][0]["lv"] = 50
        forge.max_tsum_level.reset_mock(side_effect=True)
        forge.max_tsum_level.return_value = {"status": "done"}
        result, _ = self.run_all(forge)
        self.assertEqual(result["count"], 3)
        self.assertEqual([call.args[0] for call in forge.max_tsum_level.call_args_list], [2, 3])

    def test_invalid_or_empty_info_never_reports_success(self):
        forge = self.forge([])
        for info in ({"retcode": 1, "tsuminfo": []}, {"retcode": 0}, {"retcode": 0, "tsuminfo": []},
                     {"retcode": 0, "tsuminfo": [{"getflg": 1}]}):
            with self.subTest(info=info):
                forge.get_info.return_value = info
                result, _ = self.run_all(forge)
                self.assertIsNone(result)
        forge.max_tsum_level.assert_not_called()

    def test_guest_path_executes_all_levels(self):
        forge = self.forge([{"tsumid": 7, "getflg": 1, "lv": 1}])
        session = {"userid": "test", "hash": "hash", "checkval": "check"}
        with patch.object(tsum_bot.tsum_guest, "guest_takeover", return_value=(session, None)), \
             patch.object(tsum_bot.tsum_guest, "guest_issue_transfer", return_value=("new", "pw")), \
             patch.object(tsum_bot.tsum_guest, "mark_pending_done"), \
             patch.object(tsum_bot, "_save_guest_transfer"), \
             patch.object(tsum_bot, "Forge", return_value=forge):
            result, _, info = tsum_bot.forge_sync_guest("id", "pw", tsum_all_lv=1)
        self.assertEqual(result["count"], 1)
        self.assertEqual(info["new_id"], "new")
        self.assertEqual(forge.max_tsum_level.call_args.args[0], 7)

    def test_denied_work_consent_never_levels(self):
        forge = self.forge([{"tsumid": 7, "getflg": 1, "lv": 1}])
        progress = Mock()
        progress.confirm.return_value = False
        result, text = tsum_bot.forge_sync(
            tsum_all_lv=1, sess={"userid": "test"}, f=forge, owned_progress=progress)
        self.assertIsNone(result)
        self.assertIn("同意", text)
        forge.max_tsum_level.assert_not_called()

    def test_confirmed_work_reports_heart_samples_and_completion(self):
        forge = self.forge([{"tsumid": 7, "getflg": 1, "lv": 1}])
        initial = forge.get_info.return_value
        sample = {"retcode": 0, "userinfo": {"bheart": 19}}
        forge.get_info.side_effect = [initial, sample]
        forge.max_tsum_level.side_effect = lambda *args, **kwargs: (
            kwargs["on_play"]() or {"status": "done"})
        progress = Mock()
        progress.confirm.return_value = True
        result, _ = tsum_bot.forge_sync(
            tsum_all_lv=1, sess={"userid": "test"}, f=forge, owned_progress=progress)
        self.assertEqual(result["count"], 1)
        progress.confirm.assert_called_once_with(1, 1, initial)
        self.assertEqual(progress.update.call_args_list[0].args, (0, 0, 7, sample))
        self.assertEqual(progress.update.call_args_list[1].args, (1, 1, 7))

    def test_guest_second_consent_denied_still_returns_new_transfer(self):
        forge = self.forge([{"tsumid": 7, "getflg": 1, "lv": 1}])
        progress = Mock()
        progress.confirm.return_value = False
        session = {"userid": "test", "hash": "hash", "checkval": "check"}
        with patch.object(tsum_bot.tsum_guest, "guest_takeover", return_value=(session, None)), \
             patch.object(tsum_bot.tsum_guest, "guest_issue_transfer", return_value=("new", "pw")), \
             patch.object(tsum_bot.tsum_guest, "mark_pending_done"), \
             patch.object(tsum_bot, "_save_guest_transfer"), \
             patch.object(tsum_bot, "Forge", return_value=forge):
            result, text, info = tsum_bot.forge_sync_guest("id", "pw", tsum_all_lv=1, owned_progress=progress)
        self.assertIsNone(result)
        self.assertEqual(info["new_id"], "new")
        self.assertIn("新しい引き継ぎ情報", text)
        forge.max_tsum_level.assert_not_called()

    def test_progress_callback_runs_for_failed_and_retried_play(self):
        forge = tsum_bot.Forge(proxy=None)
        forge.get_info = Mock(return_value={"retcode": 0, "userinfo": {"bheart": 20}})
        forge.level_tsum_once = Mock(side_effect=[
            None, {"retcode": 0, "tsuminfo": [{"tsumid": 7, "lv": 50}]}])
        forge.last_gs_response = None
        callback = Mock()
        with patch("tsum_forge.time.sleep"):
            result = forge.max_tsum_level(7, on_play=callback)
        self.assertEqual(result["status"], "done")
        self.assertEqual(callback.call_count, 2)

    def test_medal_max_uses_gap_to_cap(self):
        forge = Mock()
        forge.last_http_status = None
        forge.last_error = None
        forge.get_info.side_effect = [
            {
                "retcode": 0,
                "userinfo": {"bmedal": 490000},
                "tsuminfo": [{"tsumid": 7, "setflg": 1, "lv": 1, "getflg": 1}],
            },
            {
                "retcode": 0,
                "userinfo": {"bmedal": 490000},
                "tsuminfo": [{"tsumid": 7, "setflg": 1, "lv": 1, "getflg": 1}],
            },
            {
                "retcode": 0,
                "userinfo": {"bmedal": 500000},
            },
        ]
        forge.game_start.return_value = {"retcode": 0, "userinfo": {"playcode": "pc"}}
        forge.game_end.return_value = {"retcode": 0, "userinfo": {"bmedal": 500000}}
        with patch.object(tsum_bot, "_cached_event_candidates", return_value=["0"]):
            result, text = tsum_bot.forge_sync(medal_max=500000, sess={"userid": "test"}, f=forge)
        self.assertIsNotNone(result)
        self.assertIn("メダル", text)
        self.assertEqual(forge.game_end.call_args.kwargs["medal"], 10000)

    def test_medal_max_fails_when_medal_is_not_reflected(self):
        forge = Mock()
        forge.last_http_status = None
        forge.last_error = None
        forge.get_info.side_effect = [
            {"retcode": 0, "userinfo": {"bmedal": 490000}, "tsuminfo": [{"tsumid": 7, "setflg": 1}]},
            {"retcode": 0, "userinfo": {"bmedal": 490000}, "tsuminfo": [{"tsumid": 7, "setflg": 1}]},
            {"retcode": 0, "userinfo": {"bmedal": 490000}},
            {"retcode": 0, "userinfo": {"bmedal": 490000}},
            {"retcode": 0, "userinfo": {"bmedal": 490000}},
            {"retcode": 0, "userinfo": {"bmedal": 490000}},
        ]
        forge.game_start.return_value = {"retcode": 0, "userinfo": {"playcode": "pc"}}
        forge.game_end.return_value = {"retcode": 0, "userinfo": {"bmedal": 490000}}
        with patch.object(tsum_bot, "_cached_event_candidates", return_value=["0"]), \
             patch("tsum_bot.time.sleep"):
            result, text = tsum_bot.forge_sync(medal_max=500000, sess={"userid": "test"}, f=forge)
        self.assertIsNone(result)
        self.assertIn("反映を確認できません", text)

    def test_medal_out_of_range_is_rejected(self):
        forge = Mock()
        result, text = tsum_bot.forge_sync(medal=500001, sess={"userid": "test"}, f=forge)
        self.assertIsNone(result)
        self.assertIn("0〜500,000", text)

    def test_full_stack_runs_steps_in_order(self):
        forge = Mock()
        calls = []

        def _step(**kwargs):
            calls.append(kwargs)
            return {"status": "done"}, "ok"

        with patch.object(tsum_bot, "forge_sync", side_effect=_step), \
             patch.object(tsum_bot, "max_owned_tsum_levels", return_value=({"status": "done", "count": 3}, "ok")):
            result, text = tsum_bot.run_full_stack_account(forge, {"userid": "test"})

        self.assertEqual(result["status"], "done")
        self.assertIn("全盛りアカウント作成が完了しました", text)
        action_order = []
        for call in calls:
            for key in ("coin", "exp", "box", "gacha_full", "ruby"):
                if key in call:
                    action_order.append(key)
                    break
        self.assertEqual(action_order, ["coin", "exp", "box", "coin", "gacha_full", "ruby"])
        self.assertTrue(all(c["medal"] == 0 for c in calls))

    def test_full_stack_reports_heart_shortage_after_ruby_step(self):
        forge = Mock()

        with patch.object(tsum_bot, "forge_sync", return_value=({"status": "done"}, "ok")), \
             patch.object(tsum_bot, "max_owned_tsum_levels",
                          return_value=(None, "ハートが足りないため中断しました")):
            result, text = tsum_bot.run_full_stack_account(forge, {"userid": "test"})

        self.assertIsNone(result)
        self.assertIn("途中まで完了", text)
        self.assertIn("ルビーは補充済み", text)

    def test_ruby_value_is_sent_to_game_end(self):
        forge = Mock()
        forge.last_http_status = None
        forge.last_error = None
        forge.get_info.return_value = {
            "retcode": 0,
            "userinfo": {"bruby": 1200},
            "tsuminfo": [{"tsumid": 7, "setflg": 1, "lv": 1, "getflg": 1}],
        }
        forge.game_start.return_value = {"retcode": 0, "userinfo": {"playcode": "pc"}}
        forge.game_end.return_value = {"retcode": 0, "userinfo": {"bruby": 1234}}
        with patch.object(tsum_bot, "_cached_event_candidates", return_value=["0"]):
            result, text = tsum_bot.forge_sync(ruby=34, sess={"userid": "test"}, f=forge)
        self.assertIsNotNone(result)
        self.assertEqual(forge.game_end.call_args.kwargs["ruby"], 34)
        self.assertIn("ルビー", text)

    def test_ruby_is_not_sent_when_not_specified(self):
        forge = Mock()
        forge.last_http_status = None
        forge.last_error = None
        forge.get_info.return_value = {
            "retcode": 0,
            "userinfo": {"bmedal": 1000},
            "tsuminfo": [{"tsumid": 7, "setflg": 1, "lv": 1, "getflg": 1}],
        }
        forge.game_start.return_value = {"retcode": 0, "userinfo": {"playcode": "pc"}}
        forge.game_end.return_value = {"retcode": 0, "userinfo": {"bmedal": 1005}}
        with patch.object(tsum_bot, "_cached_event_candidates", return_value=["0"]):
            result, _ = tsum_bot.forge_sync(medal=5, sess={"userid": "test"}, f=forge)
        self.assertIsNotNone(result)
        self.assertNotIn("ruby", forge.game_end.call_args.kwargs)


class OwnedTsumMenuTests(unittest.IsolatedAsyncioTestCase):
    async def test_menu_actions_use_ruby_instead_of_medal(self):
        keys = [key for key, _ in tsum_bot.MENU_ACTIONS]
        self.assertIn("ruby", keys)
        self.assertIn("full_stack", keys)
        self.assertNotIn("medal", keys)
        self.assertNotIn("medal_max", keys)

    async def test_menu_opens_free_account_modal_for_all_owned_tsums(self):
        interaction = Mock()
        interaction.response.send_modal = AsyncMock()
        menu = tsum_bot.MenuSelect()
        menu._values = ["tsum_all_lv"]
        with patch.object(tsum_bot, "deny_unlicensed", new_callable=AsyncMock, return_value=False), \
             patch.dict(tsum_bot.MENU_PRICES, {"tsum_all_lv": 0}):
            await menu.callback(interaction)
        modal = interaction.response.send_modal.call_args.args[0]
        self.assertEqual((modal.action, modal.val), ("tsum_all_lv", 1))
        self.assertIsNone(modal.paypay)

    async def test_only_requester_can_consent(self):
        view = tsum_bot.OwnedTsumConsentView(42)
        interaction = Mock()
        interaction.user.id = 99
        interaction.response.send_message = AsyncMock()
        self.assertFalse(await view.interaction_check(interaction))
        interaction.user.id = 42
        interaction.response.edit_message = AsyncMock()
        self.assertTrue(await view.interaction_check(interaction))
        await view.approve.callback(interaction)
        self.assertTrue(view.approved)

    async def test_timeout_removes_buttons_and_rejects_consent(self):
        channel = Mock()
        message = Mock(edit=AsyncMock())
        channel.send = AsyncMock(return_value=message)
        with patch.object(tsum_bot.OwnedTsumConsentView, "wait", new_callable=AsyncMock, return_value=True):
            approved = await tsum_bot.request_owned_consent(channel, Mock(id=42), "確認")
        self.assertFalse(approved)
        self.assertIsNone(message.edit.call_args.kwargs["view"])

    async def test_guest_decline_does_not_start_takeover(self):
        channel = Mock()
        channel.send = AsyncMock()
        user = Mock(id=42)
        with patch.object(tsum_bot, "get_account_lock", return_value=("id", asyncio.Lock())), \
             patch.object(tsum_bot, "_job_start"), patch.object(tsum_bot, "_job_end"), \
             patch.object(tsum_bot, "request_owned_consent", new_callable=AsyncMock, return_value=False), \
             patch.object(tsum_bot, "forge_sync_guest") as guest:
            ok, _, info = await tsum_bot._do_forge_guest(channel, user, "id", "pw", tsum_all_lv=1)
        self.assertFalse(ok)
        self.assertFalse(info["moved"])
        guest.assert_not_called()

    async def test_progress_reports_estimate_and_locks_final_embed(self):
        channel = Mock()
        message = Mock()
        message.edit = AsyncMock()
        channel.send = AsyncMock(return_value=message)
        reporter = tsum_bot.OwnedTsumProgress(channel, Mock(id=42), asyncio.get_running_loop())
        initial = {"retcode": 0, "userinfo": {"bheart": 20, "pheart": 0}}
        with patch.object(tsum_bot, "request_owned_consent", new_callable=AsyncMock, return_value=True):
            approved = await asyncio.to_thread(reporter.confirm, 3, 2, initial)
        self.assertTrue(approved)
        reporter.started = time.monotonic() - 120
        reporter.update(2, 1, 7, {"retcode": 0, "userinfo": {"bheart": 17, "pheart": 0}})
        embed = reporter._embed()
        self.assertIn("2/3", embed.description)
        self.assertIn("約", embed.description)
        self.assertIn("3個", embed.description)
        await reporter.finish(False)
        await asyncio.sleep(0)
        self.assertIn("中断", message.edit.call_args.kwargs["embed"].title)

    async def test_missing_heart_sample_is_not_reported_as_zero(self):
        channel = Mock()
        channel.send = AsyncMock(return_value=Mock(edit=AsyncMock()))
        reporter = tsum_bot.OwnedTsumProgress(channel, Mock(id=42), asyncio.get_running_loop())
        with patch.object(tsum_bot, "request_owned_consent", new_callable=AsyncMock, return_value=True):
            await asyncio.to_thread(reporter.confirm, 1, 1, {"retcode": 0, "userinfo": {"bheart": 20}})
        reporter.update(0, 0, 7, None)
        self.assertIn("使用したハート: 不明", reporter._embed().description)
        await reporter.finish(False)


if __name__ == "__main__":
    unittest.main()
