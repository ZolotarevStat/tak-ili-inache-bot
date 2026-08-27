from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from contextvars import ContextVar
import tempfile
import hashlib
import hmac
import logging
import os
import secrets
from itertools import permutations
from typing import Callable

from .fixtures import import_fixtures
from .models import Bet, BetEvent, BetResult, BetType, Market, Prediction, Round
from .reporting import build_popularity_chart, build_predictions_export, build_reports
from .presentation import compact_match_label, public_event_label
from .repository import Repository
from .delivery import UnknownDeliveryError
from .telegram_api import TelegramClient, TelegramHttpError
from .transport import PreSendFailure
from .draft_store import DraftStore
from .validators import BANK, MAX_STAKE, MIN_STAKE, STAKE_STEP, ValidationError, validate_prediction

PRIVATE_TYPES = {"private"}
_reanchor_context: ContextVar[tuple[str, str, str] | None] = ContextVar("cjm_reanchor_context", default=None)


class _BestEffortTelegram:
    """Keep UI transport faults from aborting a completed domain transition."""

    def __init__(self, client: TelegramClient) -> None:
        self.client = client

    def answer_callback(self, *args, **kwargs) -> None:
        self._best_effort("answer_callback", *args, **kwargs)

    def clear_keyboard(self, *args, **kwargs) -> None:
        self._best_effort("clear_keyboard", *args, **kwargs)

    def stale_callback(self, callback_id: str) -> None:
        self._best_effort("answer_callback", callback_id, "Экран устарел. Откройте /predict.")

    def _best_effort(self, method: str, *args, **kwargs) -> None:
        try:
            getattr(self.client, method)(*args, **kwargs)
        except Exception:
            logging.getLogger(__name__).warning("telegram_ui_fail method=%s", method)

    def __getattr__(self, name):
        return getattr(self.client, name)


@dataclass
class DraftBet:
    bet_type: BetType
    events: list[BetEvent] = field(default_factory=list)
    stake: int | None = None
    # Stable only inside a draft.  It is deliberately never rendered to a
    # participant, but lets a compatible partial edit retain its stake.
    bet_id: str = ""


@dataclass
class Draft:
    round_id: str
    structure: tuple[int, int] = (0, 0)
    bets: list[DraftBet] = field(default_factory=list)
    # ``current_events`` is the durable event-first selection.  Its name is
    # retained to keep the snapshot deliberately small and backward readable.
    current_events: list[BetEvent] = field(default_factory=list)
    selected_match_id: str | None = None
    phase: str = "E1"
    draft_id: str = ""
    revision: int = 1
    chat_id: int = 0
    active_message_id: int | None = None
    # A durable intent reserves the next revision before a successor send.
    # ``active_message_id=None`` makes the predecessor stale even if the
    # process dies after Telegram accepts the send but before its id is saved.
    reanchor_pending: bool = False
    reanchor_predecessor_id: int | None = None
    expresses: list[list[BetEvent]] = field(default_factory=list)
    express_index: int = 0
    event_page: int = 0
    match_page: int = 0
    replacement: bool = False
    # ``new`` is an ordinary first coupon, ``full`` is an empty replacement,
    # and ``correction`` is a clone of the confirmed coupon.
    replacement_kind: str = "new"
    selection_slots: dict[str, str] = field(default_factory=dict)
    return_phase: str = "E1"
    pending_reset: dict | None = None
    stake_edit: list[int | None] | None = None
    stake_edit_index: int = 0


class BotService:
    def __init__(self, repository: Repository, telegram: TelegramClient, now: Callable[[], datetime], admin_ids: set[str] | None = None, tournament_chat_id: int | None = None, output_dir: str | Path = "output", draft_store: DraftStore | None = None) -> None:
        self.repository, self._delivery_telegram, self.now = repository, telegram, now
        self.telegram = _BestEffortTelegram(telegram)
        self.drafts: dict[str, Draft] = {}
        self.admin_ids, self.tournament_chat_id, self.output_dir = admin_ids or set(), tournament_chat_id, Path(output_dir)
        self.pending_imports: dict[str, Round] = {}
        # Admin close confirmations are deliberately bound to an immutable
        # round revision and a short one-shot token.  They are not Telegram
        # identifiers and are never persisted or shown in copy.
        self.pending_closures: dict[str, tuple[str, str, str]] = {}
        self.result_drafts: dict[tuple[str, str], tuple[set[Market], set[Market]]] = {}
        self._pending_outbox_tokens: dict[tuple[str, str], str] = {}
        self.draft_store = draft_store
        self._restore_drafts()

    def handle_update(self, update: dict) -> None:
        self._current_update_id = str(update.get("update_id", ""))
        if "message" in update:
            self._handle_message(update["message"])
        elif "callback_query" in update:
            self._handle_callback(update["callback_query"])

    def _actor_fingerprint(self, telegram_id: str) -> str | None:
        """Never emit a Telegram identifier; telemetry is disabled without a key."""
        key = os.environ.get("TAK_ILI_INACHE_TELEMETRY_HMAC_KEY")
        if not key:
            return None
        return hmac.new(key.encode("utf-8"), telegram_id.encode("utf-8"), hashlib.sha256).hexdigest()[:16]

    def _trace_draft(self, telegram_id: str, action: str, draft: Draft, phase_before: str, card_from: int | None, card_to: int | None, outcome: str) -> None:
        actor = self._actor_fingerprint(telegram_id)
        if actor is None:
            # Privacy fails closed: an unkeyed process must not create a
            # cross-update actor correlation key.
            return
        logging.getLogger(__name__).info(
            "cjm_draft_trace actor=%s update_id=%s action=%s draft_id=%s phase_before=%s phase_after=%s revision=%s card_from=%s card_to=%s outcome=%s",
            actor,
            self._current_update_id,
            action,
            draft.draft_id,
            phase_before,
            draft.phase,
            draft.revision,
            card_from if card_from is not None else "none",
            card_to if card_to is not None else "none",
            outcome,
        )

    def _handle_message(self, message: dict) -> None:
        chat, user = message["chat"], message["from"]
        if message.get("document"):
            self._handle_document(message)
            return
        text_parts = (message.get("text") or "").split()
        text = text_parts[0].lower() if text_parts else ""
        # Telegram commonly sends commands addressed to a specific bot as
        # `/command@bot_username` in groups. The update has already reached
        # this bot, so route the command through the same allowlist as its bare
        # form instead of treating the suffix as an unknown command.
        if text.startswith("/") and "@" in text:
            text = text.split("@", 1)[0]
        if text in {"/help", "/rules"}:
            self.telegram.send_message(chat["id"], self._help() if text == "/help" else self._rules())
            return
        if chat.get("type") not in PRIVATE_TYPES:
            if text in {"/start", "/predict", "/new", "/my", "/admin", "/status", "/publish", "/export", "/score"}:
                self.telegram.send_message(chat["id"], "Сбор и просмотр прогнозов доступны только в личном чате с ботом.")
            return
        telegram_id = str(user["id"])
        display_name = " ".join(filter(None, [user.get("first_name"), user.get("last_name")])) or user.get("username", "Участник")
        if text == "/start":
            self.repository.register_participant(telegram_id, display_name)
            self._menu(chat["id"], "Регистрация готова.")
        elif text in {"/predict", "/new"}:
            self._begin_prediction(chat["id"], telegram_id)
        elif text == "/my":
            self._show_my(chat["id"], telegram_id)
        elif text == "/admin":
            self._admin_menu(chat["id"], telegram_id)
        elif text == "/status":
            self._admin_status(chat["id"], telegram_id)
        elif text == "/publish":
            self._publish(chat["id"], telegram_id)
        elif text == "/export":
            self._export_predictions(chat["id"], telegram_id)
        elif text == "/score":
            self._score(chat["id"], telegram_id)
        elif self._draft(telegram_id) and self._draft(telegram_id).phase == "B1" and (message.get("text") or "").strip().isdigit():
            self._set_stake(chat["id"], telegram_id, int((message.get("text") or "").strip()))
        else:
            self._menu(chat["id"], "Используйте кнопки меню или /help.")

    def _handle_callback(self, query: dict) -> None:
        message, user = query["message"], query["from"]
        chat_id, telegram_id, data = message["chat"]["id"], str(user["id"]), query["data"]
        if message["chat"].get("type") not in PRIVATE_TYPES:
            self.telegram.send_message(chat_id, "Это действие доступно только в личном чате с ботом.")
            return
        if data.startswith("d:"):
            action = self._take_draft_callback(data, telegram_id, chat_id, message.get("message_id"))
            if action is None:
                self.telegram.stale_callback(query["id"])
                return
            boundary_ack = self._match_page_boundary_ack(telegram_id, action)
            self.telegram.answer_callback(query["id"], boundary_ack or "")
            if boundary_ack is not None:
                return
            data = action
        elif data.startswith("draft:"):
            # Bound recovery actions validate their own draft/revision token.
            # They must not clear a card before the deadline/token guard runs.
            self.telegram.answer_callback(query["id"])
        else:
            if self.draft_store and _is_draft_action(data):
                self.telegram.stale_callback(query["id"])
                return
            # Legacy/admin controls are intentionally one-shot UI. Draft controls
            # use the durable revision contract above and are edited in-place.
            self.telegram.answer_callback(query["id"])
            self.telegram.clear_keyboard(chat_id, message["message_id"])
        if data == "menu:new":
            self._begin_prediction(chat_id, telegram_id)
        elif data == "resume":
            self._resume_draft(chat_id, telegram_id)
        elif data.startswith("draft:resume:"):
            self._resume_from_my(chat_id, telegram_id, data)
        elif data.startswith("draft:restart:ask:"):
            self._request_draft_restart(chat_id, telegram_id, data)
        elif data.startswith("draft:restart:yes:"):
            self._confirm_draft_restart(chat_id, telegram_id, data)
        elif data.startswith("draft:restart:no:"):
            self._decline_draft_restart(chat_id, telegram_id, data)
        elif data == "menu:my":
            self._show_my(chat_id, telegram_id)
        elif data == "cancel":
            self._cancel_draft(chat_id, telegram_id)
        elif data == "cancel:yes":
            was_replacement = bool(self._draft(telegram_id) and self._draft(telegram_id).replacement)
            self._discard_draft(telegram_id)
            if was_replacement:
                self._show_my(chat_id, telegram_id)
            else:
                self._menu(chat_id, "Черновик отменён.")
        elif data == "menu:close":
            self._menu(chat_id, "")
        elif data == "deadline":
            self._deadline_screen(chat_id, telegram_id)
        elif data == "replace":
            self._render_full_replace_confirmation(chat_id, telegram_id)
        elif data == "full:start":
            self._start_replacement(chat_id, telegram_id)
        elif data == "full:back":
            self._show_my(chat_id, telegram_id)
        elif data == "correct":
            self._start_correction(chat_id, telegram_id)
        elif data == "r:events":
            self._correction_route(chat_id, telegram_id, "E1")
        elif data == "r:expresses":
            self._correction_route(chat_id, telegram_id, "X2")
        elif data == "r:amounts":
            self._start_stake_entry(chat_id, telegram_id)
        elif data == "r:my":
            self._correction_route(chat_id, telegram_id, "M1")
        elif data == "admin:menu":
            self.pending_imports.pop(telegram_id, None)
            self.result_drafts = {key: value for key, value in self.result_drafts.items() if key[0] != telegram_id}
            self._admin_menu(chat_id, telegram_id)
        elif data == "admin:csv-format":
            self._csv_format(chat_id, telegram_id)
        elif data == "back":
            self._back(chat_id, telegram_id)
        elif data.startswith("structure:"):
            self._set_structure(chat_id, telegram_id, data)
        elif data.startswith("match:"):
            self._select_match(chat_id, telegram_id, data.removeprefix("match:"))
        elif data.startswith("page:m:"):
            self._select_match(chat_id, telegram_id, data)
        elif data.startswith("market:"):
            _, match_id, market = data.split(":", 2)
            self._select_market(chat_id, telegram_id, match_id, Market(market))
        elif data.startswith("remove:"):
            self._remove_selected_match(chat_id, telegram_id, data.removeprefix("remove:"))
        elif data.startswith("page:e:"):
            self._select_market(chat_id, telegram_id, data, Market.P1)
        elif data == "finish-events":
            self._finish_bet(chat_id, telegram_id)
        elif data == "need6":
            self.telegram.send_message(chat_id, "Чтобы завершить выбор, нужно минимум 6 событий.")
        elif data == "to-express":
            self._enter_express(chat_id, telegram_id)
        elif data.startswith("schema:"):
            self._set_structure(chat_id, telegram_id, data)
        elif data.startswith("x:"):
            self._express_action(chat_id, telegram_id, data)
        elif data == "bank":
            self._start_stake_entry(chat_id, telegram_id)
        elif data == "menu:help":
            self.telegram.send_message(chat_id, self._help())
        elif data.startswith("stake:"):
            self._set_stake(chat_id, telegram_id, int(data.removeprefix("stake:")))
        elif data == "stakes:apply":
            self._apply_stake_edit(chat_id, telegram_id)
        elif data == "stakes:edit":
            self._edit_stake_again(chat_id, telegram_id)
        elif data == "stakes:cancel":
            self._cancel_stake_edit(chat_id, telegram_id)
        elif data == "warn:apply":
            self._apply_pending_reset(chat_id, telegram_id)
        elif data == "warn:cancel":
            self._cancel_pending_reset(chat_id, telegram_id)
        elif data == "confirm":
            self._confirm(chat_id, telegram_id)
        elif data == "admin:status":
            self._admin_status(chat_id, telegram_id)
        elif data == "admin:publish":
            self._publish(chat_id, telegram_id)
        elif data == "admin:export-predictions":
            self._export_predictions(chat_id, telegram_id)
        elif data == "admin:results":
            self._result_matches(chat_id, telegram_id)
        elif data == "admin:score":
            self._score(chat_id, telegram_id)
        elif data == "admin:outbox":
            self._outbox_menu(chat_id, telegram_id)
        elif data.startswith("admin:outbox-done:"):
            self._resolve_outbox(chat_id, telegram_id, data.removeprefix("admin:outbox-done:"), True)
        elif data.startswith("admin:outbox-retry:"):
            self._resolve_outbox(chat_id, telegram_id, data.removeprefix("admin:outbox-retry:"), False)
        elif data == "admin:activate":
            self._activate_pending(chat_id, telegram_id)
        elif data == "admin:replace":
            self._replace_pending(chat_id, telegram_id)
        elif data == "admin:close":
            self._request_close(chat_id, telegram_id)
        elif data.startswith("admin:close-confirm:"):
            self._confirm_close(chat_id, telegram_id, data.rsplit(":", 1)[-1])
        elif data.startswith("admin:result:"):
            self._result_editor(chat_id, telegram_id, data.rsplit(":", 1)[-1])
        elif data.startswith("admin:toggle:"):
            _, _, match_id, market = data.split(":", 3)
            self._toggle_result(chat_id, telegram_id, match_id, Market(market))
        elif data.startswith("admin:return:"):
            self._return_match(chat_id, telegram_id, data.rsplit(":", 1)[-1])
        elif data.startswith("admin:return-market:"):
            _, _, match_id, market = data.split(":", 3)
            self._toggle_return(chat_id, telegram_id, match_id, Market(market))
        elif data.startswith("admin:save-result:"):
            self._save_result(chat_id, telegram_id, data.rsplit(":", 1)[-1])

    def _handle_document(self, message: dict) -> None:
        chat, telegram_id = message["chat"], str(message["from"]["id"])
        if chat.get("type") not in PRIVATE_TYPES or not self._is_admin(telegram_id):
            self.telegram.send_message(chat["id"], "Загрузка линии доступна только администратору в личном чате.")
            return
        document = message["document"]
        if not str(document.get("file_name", "")).lower().endswith(".csv"):
            self.telegram.send_message(chat["id"], "Нужен файл fixtures CSV.")
            return
        try:
            with tempfile.NamedTemporaryFile(suffix=".csv", delete=False) as target:
                target.write(self.telegram.download_document(document))
                path = Path(target.name)
            candidate = import_fixtures(path)
        except (ValidationError, UnicodeError) as error:
            self.telegram.send_message(chat["id"], f"CSV отклонён: {error}")
            return
        except Exception:
            # Transport errors must not make the polling worker replay one document forever.
            self.telegram.send_message(chat["id"], "Не удалось скачать CSV. Отправьте файл повторно.")
            return
        finally:
            if "path" in locals():
                path.unlink(missing_ok=True)
        self.pending_imports[telegram_id] = candidate
        existing = self.repository.get_active_round()
        if existing and candidate.round_id == existing.round_id:
            message = f"Проверка пройдена: тур {candidate.round_id}, матчей {len(candidate.fixtures)}, дедлайн {candidate.deadline_msk:%d.%m %H:%M}. Обновить линию этого же тура?"
            buttons = [("Перезаписать активный тур", "admin:replace"), ("Назад в админ-меню", "admin:menu")]
        elif existing:
            self.pending_imports.pop(telegram_id, None)
            message = f"Проверка пройдена: тур {candidate.round_id}. Активен тур {existing.round_id}; сначала завершите активный тур. История и текущие данные будут сохранены."
            buttons = [("Назад в админ-меню", "admin:menu")]
        else:
            message = f"Проверка пройдена: тур {candidate.round_id}, матчей {len(candidate.fixtures)}, дедлайн {candidate.deadline_msk:%d.%m %H:%M}. Активировать?"
            buttons = [("Активировать новый тур", "admin:activate"), ("Назад в админ-меню", "admin:menu")]
        self.telegram.send_message(chat["id"], message, _keyboard(buttons))

    def _is_admin(self, telegram_id: str) -> bool:
        return telegram_id in self.admin_ids

    def _admin_menu(self, chat_id: int, telegram_id: str) -> None:
        if not self._is_admin(telegram_id):
            self.telegram.send_message(chat_id, "Команда доступна только администратору.")
            return
        round_ = self.repository.get_active_round()
        buttons = [("Формат и пример CSV", "admin:csv-format")]
        action_lines = ["📥 Загрузить тур — отправить CSV и проверить данные"]
        if self.repository.pending_operations():
            buttons.append(("Восстановить отправки", "admin:outbox"))
        if round_:
            predictions = len(self.repository.latest_predictions(round_.round_id))
            result_count = len(self.repository.results(round_.round_id))
            stage = "open" if self.now() < round_.deadline_msk else "locked"
            smoke = round_.round_id.startswith("SMOKE-")
            buttons.append(("Статус сдачи", "admin:status"))
            if predictions:
                buttons.append(("Выгрузить прогнозы CSV", "admin:export-predictions"))
                action_lines.append("📤 Выгрузить прогнозы — CSV для самостоятельной аналитики")
            if self.now() < round_.deadline_msk:
                action_lines.append("🔓 Тур открыт до дедлайна")
                if smoke:
                    stage = "open (тестовый)"
                    buttons += [("Закрыть тестовый тур без расчёта", "admin:close")]
                    action_lines.append("🧪 Тестовый тур можно закрыть без расчёта в любой момент; история сохранится")
            else:
                buttons += [("Опубликовать прогнозы", "admin:publish")]
                action_lines.append("🔒 Прогнозы можно опубликовать после дедлайна")
                if smoke:
                    stage = "results/scoring (тестовый)"
                    buttons += [("Закрыть тестовый тур без расчёта", "admin:close")]
                    action_lines.append("🧪 Тестовый тур можно закрыть без расчёта в любой момент; история сохранится")
                elif self.repository.round_scored(round_.round_id):
                    stage = "scored"
                    buttons += [("Завершить и архивировать тур", "admin:close")]
                    action_lines.append("✅ Расчёт завершён — следующий шаг: архивировать тур")
                elif result_count < len(round_.fixtures):
                    stage = "results"
                    buttons += [("Внести результаты", "admin:results")]
                    action_lines.append("🧾 Следующий шаг: внести результаты всех матчей")
                else:
                    stage = "scoring"
                    buttons += [("Скоринг", "admin:score")]
                    action_lines.append("🧮 Следующий шаг: рассчитать результаты")
        else:
            predictions = result_count = 0
            stage = "архивирован / нет активного"
        confirmed = len(self.repository.latest_predictions(round_.round_id)) if round_ else 0
        deadline = f"{round_.deadline_msk:%d.%m %H:%M} МСК" if round_ else "—"
        self.telegram.send_message(chat_id, "🛠️ Админские команды\n" + "\n".join(action_lines) + f"\n\n🏟️ Текущий тур: {round_.round_id if round_ else 'нет (последний архивирован)'}\n📍 Этап: {stage}\n⏰ Дедлайн: {deadline}\n⚽ Матчей: {len(round_.fixtures) if round_ else 0}\n📝 Подтверждено прогнозов: {confirmed}\n📊 Результаты: {result_count}/{len(round_.fixtures) if round_ else 0}\n\nВыберите доступное действие:", _keyboard(buttons))

    def _csv_format(self, chat_id: int, telegram_id: str) -> None:
        if not self._is_admin(telegram_id):
            self.telegram.send_message(chat_id, "Недостаточно прав.")
            return
        template = Path(__file__).resolve().parents[2] / "data" / "fixtures_sample.csv"
        self.telegram.send_message(chat_id, "CSV: 11–14 матчей одного round_id. Обязательные колонки: round_id, match_id, kickoff_msk (ISO с timezone или МСК), home_team, away_team, total_line, odds_p1, odds_x, odds_p2, odds_tb, odds_tm, odds_1x, odds_x2. Коэффициенты — конечные Decimal > 1. Ниже — валидный пример, замените его данными тура.")
        self.telegram.send_document(chat_id, str(template), "fixtures_example.csv")
        self.telegram.send_message(chat_id, "После подготовки отправьте CSV документом сюда.", _keyboard([("Назад в админ-меню", "admin:menu")]))

    def _outbox_menu(self, chat_id: int, telegram_id: str) -> None:
        if not self._is_admin(telegram_id):
            self.telegram.send_message(chat_id, "Недостаточно прав.")
            return
        pending = self.repository.pending_operations()
        if not pending:
            self.telegram.send_message(chat_id, "Неразрешённых отправок нет.", _keyboard([("Назад в админ-меню", "admin:menu")]))
            return
        buttons = []
        for index, operation_key in enumerate(pending):
            token = str(index)
            self._pending_outbox_tokens[(telegram_id, token)] = operation_key
            title = operation_key.rsplit(":", 1)[-1][:40]
            buttons += [(f"Подтвердить доставку: {title}", f"admin:outbox-done:{token}"), (f"Подтвердить недоставку и повторить: {title}", f"admin:outbox-retry:{token}")]
        buttons.append(("Назад в админ-меню", "admin:menu"))
        self.telegram.send_message(chat_id, "Есть незавершённые отправки. Проверьте чат получателя: отметьте доставленное или подтвердите недоставку, чтобы разрешить безопасный повтор соответствующей команды.", _keyboard(buttons))

    def _resolve_outbox(self, chat_id: int, telegram_id: str, token: str, delivered: bool) -> None:
        operation_key = self._pending_outbox_tokens.get((telegram_id, token))
        if not self._is_admin(telegram_id) or not operation_key:
            self.telegram.send_message(chat_id, "Шаг recovery не найден. Откройте «Восстановить отправки» снова.")
            return
        self.repository.resolve_operation(operation_key, delivered)
        self._pending_outbox_tokens.pop((telegram_id, token), None)
        self.telegram.send_message(chat_id, "Доставка подтверждена." if delivered else "Недоставка подтверждена; повторите /publish или /score, чтобы отправить этот шаг заново.", _keyboard([("Восстановить отправки", "admin:outbox"), ("Назад в админ-меню", "admin:menu")]))

    def _activate_pending(self, chat_id: int, telegram_id: str) -> None:
        candidate = self.pending_imports.get(telegram_id)
        if not self._is_admin(telegram_id) or not candidate:
            self.telegram.send_message(chat_id, "Нет подтверждённой линии для активации.")
            return
        existing = self.repository.get_active_round()
        if existing:
            self.telegram.send_message(chat_id, f"Активный тур {existing.round_id} уже существует; перезапись заблокирована.")
            return
        self.repository.save_round(candidate, actor_id=telegram_id, imported_at=self.now().isoformat())
        self.pending_imports.pop(telegram_id, None)
        self.telegram.send_message(chat_id, f"Тур {candidate.round_id} активирован.")

    def _replace_pending(self, chat_id: int, telegram_id: str) -> None:
        candidate = self.pending_imports.get(telegram_id)
        if not self._is_admin(telegram_id) or not candidate:
            self.telegram.send_message(chat_id, "Нет подтверждённой линии для перезаписи.")
            return
        existing = self.repository.get_active_round()
        if not existing:
            self.telegram.send_message(chat_id, "Активного тура нет: используйте «Активировать линию».")
            return
        if candidate.round_id != existing.round_id:
            self.telegram.send_message(chat_id, "Новый round_id нельзя активировать поверх текущего: сначала завершите активный тур.")
            return
        if self.repository.latest_predictions(existing.round_id) or self.repository.results(existing.round_id) or self.repository.pending_operations():
            self.telegram.send_message(chat_id, "Перезапись заблокирована: в активном туре уже есть прогнозы, результаты или незавершённые отправки.")
            return
        self.repository.replace_round(candidate, actor_id=telegram_id, imported_at=self.now().isoformat())
        self.pending_imports.pop(telegram_id, None)
        self.telegram.send_message(chat_id, f"Тур {existing.round_id} перезаписан линией {candidate.round_id}.")

    def _request_close(self, chat_id: int, telegram_id: str) -> None:
        if not self._is_admin(telegram_id):
            self.telegram.send_message(chat_id, "Недостаточно прав.")
            return
        round_ = self.repository.get_active_round()
        if not round_:
            self.telegram.send_message(chat_id, "Активного тура уже нет: он завершён или не загружен.")
            return
        if self.repository.pending_operations():
            self.telegram.send_message(chat_id, "Завершение заблокировано: сначала разрешите незавершённые отправки.")
            return
        smoke = round_.round_id.startswith("SMOKE-")
        if not smoke and self.now() < round_.deadline_msk:
            self.telegram.send_message(chat_id, "Завершение доступно только после дедлайна.")
            return
        if not smoke and not self.repository.round_scored(round_.round_id):
            self.telegram.send_message(chat_id, "Обычный тур можно завершить только после успешного скоринга.")
            return
        token = secrets.token_hex(4)
        self.pending_closures[telegram_id] = (round_.round_id, round_.checksum, token)
        text = (f"Закрыть тестовый тур {round_.round_id} без расчёта? Это тестовый тур: публикации, частичные результаты, прогнозы и audit останутся в истории."
                if smoke else f"Завершить и архивировать тур {round_.round_id}? История и отчёты сохранятся.")
        self.telegram.send_message(chat_id, text, _keyboard([("Подтвердить завершение", f"admin:close-confirm:{token}"), ("Назад в админ-меню", "admin:menu")]))

    def _confirm_close(self, chat_id: int, telegram_id: str, token: str) -> None:
        pending = self.pending_closures.get(telegram_id)
        if not self._is_admin(telegram_id) or not pending or not token:
            self.telegram.send_message(chat_id, "Подтверждение завершения устарело. Откройте /admin.")
            return
        round_id, checksum, expected_token = pending
        if token != expected_token:
            self.telegram.send_message(chat_id, "Подтверждение завершения устарело. Откройте /admin.")
            return
        round_ = self.repository.get_active_round()
        if not round_ or round_.round_id != round_id or round_.checksum != checksum:
            self.pending_closures.pop(telegram_id, None)
            self.telegram.send_message(chat_id, "Тур уже завершён; история сохранена.")
            return
        if self.repository.pending_operations():
            self.telegram.send_message(chat_id, "Завершение заблокировано: появились незавершённые отправки.")
            return
        if (not round_id.startswith("SMOKE-") and (self.now() < round_.deadline_msk or not self.repository.round_scored(round_id))):
            self.telegram.send_message(chat_id, "Условия завершения больше не выполнены.")
            return
        self.repository.close_round(round_id, actor_id=telegram_id, closed_at=self.now().isoformat())
        self.pending_closures.pop(telegram_id, None)
        self.telegram.send_message(chat_id, f"Тур {round_id} завершён и архивирован. Можно загрузить CSV следующего тура.")

    def _admin_status(self, chat_id: int, telegram_id: str) -> None:
        if not self._is_admin(telegram_id):
            self.telegram.send_message(chat_id, "Недостаточно прав.")
            return
        round_ = self.repository.get_active_round()
        if not round_:
            self.telegram.send_message(chat_id, "Активного тура нет.")
            return
        submitted = {item.participant_id for item in self.repository.latest_predictions(round_.round_id)}
        people = self.repository.participants()
        done = [item.display_name for item in people if item.participant_id in submitted]
        missing = [item.display_name for item in people if item.participant_id not in submitted]
        self.telegram.send_message(chat_id, f"Тур {round_.round_id}: сдали {len(done)}/{len(people)}.\nСдали: {', '.join(done) or '—'}\nНе сдали: {', '.join(missing) or '—'}")

    def _publish(self, chat_id: int, telegram_id: str) -> None:
        if not self._is_admin(telegram_id):
            self.telegram.send_message(chat_id, "Недостаточно прав.")
            return
        round_ = self.repository.get_active_round()
        if not round_ or self.now() < round_.deadline_msk:
            self.telegram.send_message(chat_id, "Публикация возможна только после общего дедлайна.")
            return
        if self.tournament_chat_id is None:
            self.telegram.send_message(chat_id, "TOURNAMENT_CHAT_ID не настроен.")
            return
        predictions = self.repository.latest_predictions(round_.round_id)
        names = {item.participant_id: item.display_name for item in self.repository.participants()}
        try:
            chart_path, popularity_rows = build_popularity_chart(self.output_dir, round_, predictions)
        except Exception as error:
            logging.getLogger(__name__).exception("publish_chart_failed round_id=%s", round_.round_id)
            self.telegram.send_message(chat_id, f"Публикация не начата: не удалось собрать инфографику ({type(error).__name__}).")
            return
        revision = hashlib.sha256(repr(("v2-human-readable", round_.checksum, predictions, sorted(names.items()))).encode()).hexdigest()[:16]
        operation_key = f"publish-v2:{round_.round_id}:{revision}"
        if self.repository.operation_done(operation_key):
            self.telegram.send_message(chat_id, "Эта публикация уже обработана.")
            return
        for index, prediction in enumerate(predictions, 1):
            lines = [f"Купон: {names.get(prediction.participant_id, prediction.participant_id)}"]
            for bet in prediction.bets:
                events = ", ".join(public_event_label(round_, event, include_odds=True) for event in bet.events)
                lines.append(f"{bet.stake}: {events}")
            if not self._send_once(f"{operation_key}:coupon:{index}", lambda text="\n".join(lines): self._delivery_telegram.send_message(self.tournament_chat_id, text)):
                self._recovery_notice(chat_id)
                return
        top = popularity_rows[:10]
        stats = ["Топ-10 самых популярных событий:"]
        stats += [f"{index}. {item['label']} — {item['count']}" for index, item in enumerate(top, 1)]
        if not top:
            stats.append("Нет подтверждённых прогнозов.")
        stats.append("Полная статистика — на инфографике ниже.")
        if not self._send_once(f"{operation_key}:stats", lambda text="\n".join(stats): self._delivery_telegram.send_message(self.tournament_chat_id, text)):
            self._recovery_notice(chat_id)
            return
        if not self._send_once(f"{operation_key}:stats-chart", lambda: self._delivery_telegram.send_photo(self.tournament_chat_id, str(chart_path), "Полная статистика выбора событий")):
            self._recovery_notice(chat_id)
            return
        self.repository.mark_operation_done(operation_key)
        self.telegram.send_message(chat_id, "Купоны, топ-10 событий и полная PNG-инфографика опубликованы.")

    def _export_predictions(self, chat_id: int, telegram_id: str) -> None:
        if not self._is_admin(telegram_id):
            self.telegram.send_message(chat_id, "Недостаточно прав.")
            return
        round_ = self.repository.get_active_round()
        if not round_:
            self.telegram.send_message(chat_id, "Активного тура нет.")
            return
        predictions = self.repository.latest_predictions(round_.round_id)
        if not predictions:
            self.telegram.send_message(chat_id, "В активном туре пока нет подтверждённых прогнозов.")
            return
        path = build_predictions_export(self.output_dir, round_, predictions, self.repository.participants())
        self._delivery_telegram.send_document(
            chat_id,
            str(path),
            f"predictions_{round_.round_id}.csv",
        )
        self.telegram.send_message(
            chat_id,
            "CSV выгружен в длинном формате: игрок → ставка → событие. Telegram ID в файл не включён.",
            _keyboard([("Назад в админ-меню", "admin:menu")]),
        )

    def _result_matches(self, chat_id: int, telegram_id: str) -> None:
        if not self._is_admin(telegram_id):
            self.telegram.send_message(chat_id, "Недостаточно прав.")
            return
        round_ = self.repository.get_active_round()
        if not round_:
            self.telegram.send_message(chat_id, "Активного тура нет.")
            return
        saved = {item.match_id for item in self.repository.results(round_.round_id)}
        buttons = [(f"{'✓ ' if item.match_id in saved else ''}{item.home_team} — {item.away_team}", f"admin:result:{item.match_id}") for item in round_.fixtures]
        buttons.append(("Назад в админ-меню", "admin:menu"))
        self.telegram.send_message(chat_id, "Выберите матч для результата:", _keyboard(buttons))

    def _result_editor(self, chat_id: int, telegram_id: str, match_id: str) -> None:
        if not self._is_admin(telegram_id):
            return
        winners, returns = self.result_drafts.get((telegram_id, match_id), (set(), set()))
        buttons = [(f"{'✓ ' if market in winners else ''}{market.value}", f"admin:toggle:{match_id}:{market.value}") for market in Market]
        buttons += [(f"{'↩ ' if market in returns else ''}Возврат {market.value}", f"admin:return-market:{match_id}:{market.value}") for market in Market]
        buttons += [("Возврат всех рынков", f"admin:return:{match_id}"), ("Сохранить результат", f"admin:save-result:{match_id}"), ("Назад к матчам", "admin:results")]
        self.telegram.send_message(chat_id, f"{match_id}: выберите зашедшие рынки. Выбрано: {', '.join(item.value for item in winners) or '—'}; возврат: {', '.join(item.value for item in returns) or '—'}. Для исхода сохраните канонический набор: П1+1Х, Х+1Х+Х2 или П2+Х2; выберите ровно один ТБ/ТМ. Возврат отдельного рынка — кнопка «Возврат …».", _keyboard(buttons))

    def _toggle_result(self, chat_id: int, telegram_id: str, match_id: str, market: Market) -> None:
        winners, returns = self.result_drafts.setdefault((telegram_id, match_id), (set(), set()))
        if market in winners:
            winners.remove(market)
        else:
            winners.add(market)
        self._result_editor(chat_id, telegram_id, match_id)

    def _return_match(self, chat_id: int, telegram_id: str, match_id: str) -> None:
        self.result_drafts[(telegram_id, match_id)] = (set(), set(Market))
        self._result_editor(chat_id, telegram_id, match_id)

    def _toggle_return(self, chat_id: int, telegram_id: str, match_id: str, market: Market) -> None:
        winners, returns = self.result_drafts.setdefault((telegram_id, match_id), (set(), set()))
        if market in returns:
            returns.remove(market)
        else:
            returns.add(market)
        self._result_editor(chat_id, telegram_id, match_id)

    def _save_result(self, chat_id: int, telegram_id: str, match_id: str) -> None:
        if not self._is_admin(telegram_id):
            return
        round_ = self.repository.get_active_round()
        if not round_ or match_id not in {item.match_id for item in round_.fixtures}:
            self.telegram.send_message(chat_id, "Матч не относится к активному туру.")
            return
        winners, returns = self.result_drafts.get((telegram_id, match_id), (set(), set()))
        try:
            from .validators import ensure_results_compatible
            ensure_results_compatible(winners, returns)
        except ValidationError as error:
            self.telegram.send_message(chat_id, f"Результат не сохранён: {error}")
            return
        self.repository.save_result(BetResult(match_id, frozenset(winners), frozenset(returns)), round_.round_id)
        self.result_drafts.pop((telegram_id, match_id), None)
        self.telegram.send_message(chat_id, f"Результат {match_id} сохранён.")

    def _score(self, chat_id: int, telegram_id: str) -> None:
        if not self._is_admin(telegram_id):
            self.telegram.send_message(chat_id, "Недостаточно прав.")
            return
        round_ = self.repository.get_active_round()
        if not round_:
            self.telegram.send_message(chat_id, "Активного тура нет.")
            return
        results = self.repository.results(round_.round_id)
        if {item.match_id for item in results} != {item.match_id for item in round_.fixtures}:
            self.telegram.send_message(chat_id, "Скоринг заблокирован: внесены результаты не для всех матчей.")
            return
        paths = build_reports(self.output_dir, round_.round_id, self.repository.latest_predictions(round_.round_id), results, self.repository.participants(), self.now())
        operation_key = f"score:{round_.round_id}:{paths['scoring'].parent.name}"
        if self.repository.operation_done(operation_key):
            self.telegram.send_message(chat_id, "Этот scoring уже обработан.")
            return
        if not self._send_once(f"{operation_key}:admin-scoring", lambda: self._delivery_telegram.send_document(chat_id, str(paths["scoring"]), "scoring.csv")) or not self._send_once(f"{operation_key}:admin-board", lambda: self._delivery_telegram.send_document(chat_id, str(paths["leaderboard"]), "leaderboard.csv")):
            self._recovery_notice(chat_id)
            return
        if self.tournament_chat_id is not None:
            import csv
            with paths["leaderboard"].open(encoding="utf-8", newline="") as source:
                rows = list(csv.DictReader(source))
            ranking_lines = ["Рейтинг рассчитан:"] + [f"{row['rank']}. {row['display_name']} — {row['gross_payout']}" for row in rows]
            for index, chunk in enumerate(_chunks(ranking_lines, 3500), 1):
                if not self._send_once(f"{operation_key}:ranking:{index}", lambda text="\n".join(chunk): self._delivery_telegram.send_message(self.tournament_chat_id, text)):
                    self._recovery_notice(chat_id)
                    return
            if not self._send_once(f"{operation_key}:group-board", lambda: self._delivery_telegram.send_document(self.tournament_chat_id, str(paths["leaderboard"]), "leaderboard.csv")):
                self._recovery_notice(chat_id)
                return
            chart_captions = {
                "chart_leaderboard": "Рейтинг валовых выплат",
                "chart_bet_types": "Выплаты по типам ставок",
                "chart_popularity": "Популярность событий и распределение банка",
            }
            for key in ("chart_leaderboard", "chart_bet_types", "chart_popularity"):
                if not self._send_once(f"{operation_key}:{key}", lambda key=key: self._delivery_telegram.send_photo(self.tournament_chat_id, str(paths[key]), chart_captions[key])):
                    self._recovery_notice(chat_id)
                    return
        self.repository.mark_operation_done(operation_key)
        self.repository.mark_round_scored(round_.round_id, self.now().isoformat())
        self.telegram.send_message(chat_id, "Скоринг завершён: totals scoring.csv и leaderboard.csv сверены.")

    def _send_once(self, operation_key: str, send) -> bool:
        state = self.repository.begin_operation(operation_key)
        if state == "done":
            return True
        if state != "new":
            # A prior worker may have sent but died before durable acknowledgement.
            # Never auto-retry it: an admin must reconcile the recipient side.
            return False
        send()
        self.repository.mark_operation_done(operation_key)
        return True

    def _recovery_notice(self, chat_id: int) -> None:
        self.telegram.send_message(chat_id, "Отправка остановлена для защиты от дубля. Откройте /admin → «Восстановить отправки»: подтвердите доставку или недоставку каждого шага.")

    def _restore_drafts(self) -> None:
        if not self.draft_store:
            return
        # The store is intentionally queried only for participants known to the
        # repository; orphaned/corrupt snapshots are harmlessly ignored.
        for participant in self.repository.participants():
            snapshot = self.draft_store.load(participant.telegram_id)
            if not snapshot:
                continue
            try:
                draft = self._draft_from_snapshot(snapshot)
                round_ = self.repository.get_active_round()
                if not round_ or round_.round_id != draft.round_id:
                    self.draft_store.delete(participant.telegram_id)
                    continue
                if self.now() >= round_.deadline_msk:
                    draft.phase = "L0"
                self.drafts[participant.telegram_id] = draft
                if draft.phase == "L0":
                    self._save_draft(participant.telegram_id)
            except (KeyError, TypeError, ValueError):
                self.draft_store.delete(participant.telegram_id)

    def _take_draft_callback(self, data: str, telegram_id: str, chat_id: int, message_id: int | None) -> str | None:
        try:
            _prefix, draft_id, revision, action = data.split(":", 3)
            revision_value = int(revision)
        except (TypeError, ValueError):
            return None
        draft = self._draft(telegram_id)
        if not draft or draft.draft_id != draft_id or draft.revision != revision_value or draft.chat_id != chat_id or draft.active_message_id != message_id:
            return None
        round_ = self.repository.get_active_round()
        if not round_ or round_.round_id != draft.round_id or self.now() >= round_.deadline_msk:
            self._discard_draft(telegram_id)
            return None
        # Increment before rendering so every successor card has a new contract.
        draft.revision += 1
        return action

    def _save_draft(self, telegram_id: str) -> None:
        draft = self.drafts.get(telegram_id)
        if self.draft_store and draft:
            self.draft_store.save(telegram_id, self._draft_snapshot(telegram_id, draft))

    def _discard_draft(self, telegram_id: str) -> None:
        self.drafts.pop(telegram_id, None)
        if self.draft_store:
            self.draft_store.delete(telegram_id)

    def _draft_snapshot(self, telegram_id: str, draft: Draft) -> dict:
        return {
            "draft_id": draft.draft_id,
            "participant_id": telegram_id,
            "chat_id": draft.chat_id,
            "round_id": draft.round_id,
            "state": "active",
            "revision": draft.revision,
            "active_message_id": draft.active_message_id,
            "structure": list(draft.structure),
            "bets": [{"type": bet.bet_type.value, "stake": bet.stake, "events": [self._event_snapshot(event) for event in bet.events]} for bet in draft.bets],
            "current_events": [self._event_snapshot(event) for event in draft.current_events],
            "selected_match_id": draft.selected_match_id,
            "phase": draft.phase,
        }

    @staticmethod
    def _event_snapshot(event: BetEvent) -> dict:
        return {"match_id": event.match_id, "market": event.market.value, "odds": str(event.odds_snapshot), "total_line": str(event.total_line_snapshot) if event.total_line_snapshot is not None else None}

    def _draft_from_snapshot(self, value: dict) -> Draft:
        def event(item: dict) -> BetEvent:
            return BetEvent(item["match_id"], Market(item["market"]), Decimal(item["odds"]), Decimal(item["total_line"]) if item["total_line"] is not None else None)
        bets = [DraftBet(BetType(item["type"]), [event(part) for part in item["events"]], item["stake"]) for item in value["bets"]]
        return Draft(value["round_id"], tuple(value["structure"]), bets, [event(item) for item in value["current_events"]], value["selected_match_id"], value["phase"], value["draft_id"], value["revision"], value["chat_id"], value["active_message_id"])

    def _draft_keyboard(self, draft: Draft, buttons: list[tuple[str, str]] | list[list[tuple[str, str]]], back: bool = False, cancel: bool = False) -> dict:
        prefix = f"d:{draft.draft_id}:{draft.revision}:"
        rows = buttons if buttons and isinstance(buttons[0], list) else [[item] for item in buttons]  # type: ignore[index]
        encoded_rows = [[(label, prefix + action) for label, action in row] for row in rows]  # type: ignore[arg-type]
        if back:
            encoded_rows.append([("← Назад", prefix + "back")])
        if cancel:
            encoded_rows.append([("✖️ Отменить", prefix + "cancel")])
        if any(len(value) > 64 for row in encoded_rows for _, value in row):
            raise ValueError("Callback data is too long.")
        if sum(len(row) for row in encoded_rows) > 15:
            raise ValueError("Too many inline buttons.")
        return {"inline_keyboard": [[{"text": label, "callback_data": value} for label, value in row] for row in encoded_rows]}

    def _draft_screen(self, chat_id: int, telegram_id: str, text: str, buttons: list[tuple[str, str]] | list[list[tuple[str, str]]], back: bool = False, cancel: bool = False) -> None:
        draft = self._draft(telegram_id)
        if not draft:
            return
        reanchor = _reanchor_context.get()
        if reanchor and reanchor[0] == draft.draft_id:
            self._reanchor_draft_screen(chat_id, telegram_id, text, buttons, back, cancel, reanchor[1], reanchor[2])
            return
        markup = self._draft_keyboard(draft, buttons, back, cancel)
        if draft.active_message_id is not None:
            try:
                self._delivery_telegram.edit_message(chat_id, draft.active_message_id, text, markup)
                self._save_draft(telegram_id)
                return
            except UnknownDeliveryError:
                # Telegram may already have applied the edit. A fallback send
                # would create a second live card and repeat the side effect.
                logging.getLogger(__name__).warning("telegram_draft_edit_unknown no_fallback=true")
                raise
            except PreSendFailure as error:
                logging.getLogger(__name__).warning(
                    "telegram_draft_edit_rejected fallback=send_message exception=%s",
                    type(error).__name__,
                )
            except TelegramHttpError as error:
                if error.kind == "message_not_modified":
                    self._save_draft(telegram_id)
                    return
                # A normal Bot API edit rejection is a received, deterministic
                # response. Rate limits and server faults remain observable.
                if error.status not in {200, 400}:
                    raise
                logging.getLogger(__name__).warning(
                    "telegram_draft_edit_rejected fallback=send_message exception=%s",
                    type(error).__name__,
                )
        message_id = self._delivery_telegram.send_message(chat_id, text, markup)
        if isinstance(message_id, int):
            draft.active_message_id = message_id
        self._save_draft(telegram_id)

    def _reanchor_draft_screen(self, chat_id: int, telegram_id: str, text: str, buttons: list[tuple[str, str]] | list[list[tuple[str, str]]], back: bool, cancel: bool, action: str, phase_before: str) -> None:
        """Durably invalidate then send one successor without blind retry."""
        draft = self._draft(telegram_id)
        if not draft:
            return
        card_from, revision_before = draft.active_message_id, draft.revision
        pending_before, predecessor_before = draft.reanchor_pending, draft.reanchor_predecessor_id
        draft.revision = revision_before + 1
        draft.active_message_id = None
        draft.reanchor_pending = True
        draft.reanchor_predecessor_id = card_from
        markup = self._draft_keyboard(draft, buttons, back, cancel)
        try:
            # This intent is the crash boundary: after it reaches disk, the
            # predecessor can no longer pass the revision/card guard.
            self._save_draft(telegram_id)
        except Exception:
            draft.revision, draft.active_message_id = revision_before, card_from
            draft.reanchor_pending, draft.reanchor_predecessor_id = pending_before, predecessor_before
            self._trace_draft(telegram_id, action, draft, phase_before, card_from, None, "intent_save_failed")
            raise
        if card_from is not None:
            self.telegram.clear_keyboard(chat_id, card_from)
        try:
            message_id = self._delivery_telegram.send_message(chat_id, text, markup)
        except UnknownDeliveryError:
            self._trace_draft(telegram_id, action, draft, phase_before, card_from, None, "unknown_delivery")
            raise
        except Exception:
            self._trace_draft(telegram_id, action, draft, phase_before, card_from, None, "send_failed")
            raise
        if not isinstance(message_id, int):
            self._trace_draft(telegram_id, action, draft, phase_before, card_from, None, "missing_message_id")
            raise RuntimeError("Telegram successor card has no message_id")
        draft.active_message_id = message_id
        draft.reanchor_pending = False
        draft.reanchor_predecessor_id = None
        try:
            self._save_draft(telegram_id)
        except Exception:
            # Keep the durable intent authoritative. The accepted successor
            # may exist, but its id was not committed and must not be retried.
            draft.active_message_id = None
            draft.reanchor_pending = True
            draft.reanchor_predecessor_id = card_from
            self.telegram.clear_keyboard(chat_id, message_id)
            self._trace_draft(telegram_id, action, draft, phase_before, card_from, message_id, "successor_commit_failed")
            raise
        self._trace_draft(telegram_id, action, draft, phase_before, card_from, message_id, "success")

    def _begin_prediction(self, chat_id: int, telegram_id: str) -> None:
        round_ = self.repository.get_active_round()
        if not round_:
            self.telegram.send_message(chat_id, "Сейчас нет активного тура.")
            return
        if self.now() >= round_.deadline_msk:
            self._discard_draft(telegram_id)
            self.telegram.send_message(chat_id, "Дедлайн тура уже прошёл: новый прогноз и замена закрыты.")
            return
        draft = self._draft(telegram_id)
        if draft and draft.round_id == round_.round_id and draft.chat_id == chat_id:
            self._draft_screen(chat_id, telegram_id, f"Тур {round_.round_id}, дедлайн: {round_.deadline_msk:%d.%m %H:%M}. Продолжите сбор купона.", [("Продолжить", "resume")], cancel=True)
            return
        if draft:
            # A persisted card belongs to an archived/previous round and must
            # never be resumed into the new round.
            self._discard_draft(telegram_id)
        self.drafts[telegram_id] = Draft(round_.round_id, (0, 0), phase="structure", draft_id=secrets.token_hex(4), chat_id=chat_id)
        self._save_draft(telegram_id)
        self._draft_screen(chat_id, telegram_id, f"Тур {round_.round_id}, дедлайн: {round_.deadline_msk:%d.%m %H:%M}. Выберите структуру:", [("4 ординара + 1 экспресс", "structure:4+1"), ("3 ординара + 2 экспресса", "structure:3+2")], cancel=True)

    def _resume_draft(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft:
            self._menu(chat_id, "Черновик не найден.")
        elif draft.phase == "structure":
            self._draft_screen(chat_id, telegram_id, "Выберите структуру:", [("4 ординара + 1 экспресс", "structure:4+1"), ("3 ординара + 2 экспресса", "structure:3+2")], cancel=True)
        elif draft.phase == "stake":
            self._render_stake(chat_id, telegram_id)
        elif draft.selected_match_id:
            self._select_match(chat_id, telegram_id, draft.selected_match_id)
        else:
            self._render_matches(chat_id, telegram_id)

    def _set_structure(self, chat_id: int, telegram_id: str, data: str) -> None:
        draft = self._draft(telegram_id)
        if not draft:
            return
        draft.structure = (4, 1) if data == "structure:4+1" else (3, 2)
        self._save_draft(telegram_id)
        self._render_matches(chat_id, telegram_id)

    def _render_matches(self, chat_id: int, telegram_id: str) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_:
            return
        used = {event.match_id for bet in draft.bets for event in bet.events} | {event.match_id for event in draft.current_events}
        current_type = BetType.SINGLE if len(draft.bets) < draft.structure[0] else BetType.EXPRESS
        available = [fixture for fixture in round_.fixtures if fixture.match_id not in used]
        label = "ординар" if current_type == BetType.SINGLE else f"экспресс, плечо {len(draft.current_events) + 1}"
        self._draft_screen(chat_id, telegram_id, f"Ставка {len(draft.bets) + 1}/5 — {label}. Выберите матч:", [(f"{item.home_team} — {item.away_team}", f"match:{item.match_id}") for item in available], back=True, cancel=True)

    def _select_match(self, chat_id: int, telegram_id: str, match_id: str) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_:
            return
        used = {event.match_id for bet in draft.bets for event in bet.events} | {event.match_id for event in draft.current_events}
        fixture = next((item for item in round_.fixtures if item.match_id == match_id), None)
        if not fixture or match_id in used:
            self.telegram.send_message(chat_id, "Этот матч уже использован или недоступен. Выберите другой.")
            self._render_matches(chat_id, telegram_id)
            return
        draft.selected_match_id = match_id
        self._save_draft(telegram_id)
        labels = []
        for market, odds in fixture.odds.items():
            market_label = f"{market.value} {fixture.total_line}" if market in {Market.TB, Market.TM} else market.value
            labels.append((f"{market_label} · {odds}", f"market:{match_id}:{market.value}"))
        self._draft_screen(chat_id, telegram_id, f"{fixture.home_team} — {fixture.away_team}. Выберите рынок:", labels, back=True, cancel=True)

    def _select_market(self, chat_id: int, telegram_id: str, match_id: str, market: Market) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_:
            return
        fixture = next((item for item in round_.fixtures if item.match_id == match_id), None)
        if not fixture or match_id != draft.selected_match_id:
            self._render_matches(chat_id, telegram_id)
            return
        event = BetEvent(match_id, market, fixture.odds[market], fixture.total_line if market in {Market.TB, Market.TM} else None)
        current_type = BetType.SINGLE if len(draft.bets) < draft.structure[0] else BetType.EXPRESS
        draft.current_events.append(event)
        self._save_draft(telegram_id)
        if current_type == BetType.SINGLE:
            self._finish_bet(chat_id, telegram_id)
        elif len(draft.current_events) == 3:
            self._finish_bet(chat_id, telegram_id)
        else:
            self._draft_screen(chat_id, telegram_id, f"В экспрессе {len(draft.current_events)} плечо(а). Добавить матч или завершить экспресс?", [("Добавить плечо", "express:add"), ("Завершить экспресс", "express:done")], back=True, cancel=True)

    def _finish_bet(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft:
            return
        current_type = BetType.SINGLE if len(draft.bets) < draft.structure[0] else BetType.EXPRESS
        if current_type == BetType.EXPRESS and len(draft.current_events) < 2:
            self.telegram.send_message(chat_id, "Экспресс содержит минимум два события.")
            return
        draft.bets.append(DraftBet(current_type, list(draft.current_events)))
        draft.current_events, draft.selected_match_id = [], None
        self._save_draft(telegram_id)
        if len(draft.bets) == 5:
            draft.phase = "stake"
            self._render_stake(chat_id, telegram_id)
        else:
            self._render_matches(chat_id, telegram_id)

    def _render_stake(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft:
            return
        index = next(i for i, bet in enumerate(draft.bets) if bet.stake is None)
        remaining = BANK - sum(bet.stake or 0 for bet in draft.bets)
        later = len(draft.bets) - index - 1
        minimum, maximum = max(MIN_STAKE, remaining - later * MAX_STAKE), min(MAX_STAKE, remaining - later * MIN_STAKE)
        amounts = list(range(minimum, maximum + 1, STAKE_STEP))
        self._draft_screen(chat_id, telegram_id, f"Ставка {index + 1}/5. Остаток банка: {remaining}. Выберите сумму:", [(str(value), f"stake:{value}") for value in amounts], back=True, cancel=True)

    def _set_stake(self, chat_id: int, telegram_id: str, stake: int) -> None:
        draft = self._draft(telegram_id)
        if not draft:
            return
        current = next((bet for bet in draft.bets if bet.stake is None), None)
        if not current or not MIN_STAKE <= stake <= MAX_STAKE or stake % STAKE_STEP:
            self.telegram.send_message(chat_id, "Недопустимая сумма ставки.")
            return
        current.stake = stake
        self._save_draft(telegram_id)
        if any(bet.stake is None for bet in draft.bets):
            self._render_stake(chat_id, telegram_id)
        else:
            self._preview(chat_id, telegram_id)

    def _preview(self, chat_id: int, telegram_id: str) -> None:
        prediction = self._prediction_from_draft(telegram_id)
        if not prediction:
            return
        lines = ["Проверьте купон:"]
        for index, bet in enumerate(prediction.bets, 1):
            odds = _combined_odds(bet)
            events = ", ".join(f"{event.match_id} {event.market.value}" for event in bet.events)
            potential = (Decimal(bet.stake) * odds).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
            lines.append(f"{index}. {events} — {bet.stake} × {odds} = {potential}")
        lines.append("Банк: 5 000.")
        self._draft_screen(chat_id, telegram_id, "\n".join(lines), [("Подтвердить", "confirm")], back=True, cancel=True)

    def _confirm(self, chat_id: int, telegram_id: str) -> None:
        prediction, round_ = self._prediction_from_draft(telegram_id), self.repository.get_active_round()
        if not prediction or not round_:
            return
        try:
            validate_prediction(prediction, round_, self.now())
        except ValidationError as error:
            self.telegram.send_message(chat_id, f"Купон не сохранён: {error}")
            return
        created = self.repository.save_prediction(prediction, self._current_update_id)
        self._discard_draft(telegram_id)
        self._menu(chat_id, "Прогноз сохранён. До дедлайна его можно заменить полностью." if created else "Этот update уже был обработан: актуальный прогноз восстановлен.")

    def _show_my(self, chat_id: int, telegram_id: str) -> None:
        participant, round_ = self.repository.get_participant(telegram_id), self.repository.get_active_round()
        if not participant:
            self.telegram.send_message(chat_id, "Сначала выполните /start.")
            return
        if not round_:
            self.telegram.send_message(chat_id, "Вы зарегистрированы. Сейчас нет активного тура.")
            return
        prediction = self.repository.get_prediction(round_.round_id, participant.participant_id)
        if not prediction:
            self.telegram.send_message(chat_id, "Прогноз ещё не сохранён.", _keyboard([("Собрать прогноз", "menu:new")]))
            return
        lines = ["Ваш сохранённый прогноз:"] + [f"{i}. {bet.stake}: " + ", ".join(f"{event.match_id} {event.market.value}" for event in bet.events) for i, bet in enumerate(prediction.bets, 1)]
        buttons = [("Заменить прогноз", "menu:new")] if self.now() < round_.deadline_msk else []
        self.telegram.send_message(chat_id, "\n".join(lines), _keyboard(buttons) if buttons else None)

    def _back(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft:
            self._menu(chat_id, "Черновик не найден.")
            return
        if draft.phase == "stake":
            for bet in reversed(draft.bets):
                if bet.stake is not None:
                    bet.stake = None
                    break
            self._save_draft(telegram_id)
            self._render_stake(chat_id, telegram_id)
        elif draft.current_events:
            draft.current_events.pop()
            self._save_draft(telegram_id)
            self._render_matches(chat_id, telegram_id)
        elif draft.bets:
            draft.bets.pop()
            self._save_draft(telegram_id)
            self._render_matches(chat_id, telegram_id)
        else:
            self._discard_draft(telegram_id)
            self._menu(chat_id, "Черновик отменён.")

    def _prediction_from_draft(self, telegram_id: str) -> Prediction | None:
        draft = self._draft(telegram_id)
        if not draft or len(draft.bets) != 5 or any(bet.stake is None for bet in draft.bets):
            return None
        participant = self.repository.get_participant(telegram_id)
        if not participant:
            return None
        return Prediction(draft.round_id, participant.participant_id, tuple(Bet(bet.bet_type, bet.stake or 0, tuple(bet.events)) for bet in draft.bets), self.now())

    def _draft(self, telegram_id: str) -> Draft | None:
        return self.drafts.get(telegram_id)

    def _menu(self, chat_id: int, prefix: str) -> None:
        round_ = self.repository.get_active_round()
        if not round_:
            self.telegram.send_message(chat_id, f"{prefix}\nСейчас нет активного тура. Правила доступны по /help.")
            return
        buttons = [("Мой прогноз", "menu:my")]
        if self.now() < round_.deadline_msk:
            buttons.insert(0, ("Собрать прогноз", "menu:new"))
        self.telegram.send_message(chat_id, f"{prefix}\nВыберите действие:", _keyboard(buttons))

    @staticmethod
    def _help() -> str:
        return "Как играть: /start зарегистрирует вас; /predict — соберите купон; /my — покажет сохранённую версию. Выберите 4+1 или 3+2, ровно 5 ставок на 5 000 (500–2 500, шаг 50), не повторяя матч. До общего дедлайна прогноз можно заменить полностью; после него изменения закрыты. Если сценарий прервался — откройте /my или начните новый купон."

    @staticmethod
    def _rules() -> str:
        return "Ровно 5 ставок на 5 000: структуры 4+1 или 3+2. Ставка 500–2 500, шаг 50. Матч нельзя повторять."

    # CJM v1 participant flow.  The legacy admin methods above deliberately
    # stay untouched; these methods replace only participant-draft behaviour.
    def _take_draft_callback(self, data: str, telegram_id: str, chat_id: int, message_id: int | None) -> str | None:
        try:
            _, draft_id, revision, action = data.split(":", 3)
            revision_value = int(revision)
        except (TypeError, ValueError):
            return None
        draft = self._draft(telegram_id)
        if not draft or (draft.draft_id, draft.revision, draft.chat_id, draft.active_message_id) != (draft_id, revision_value, chat_id, message_id):
            return None
        round_ = self.repository.get_active_round()
        if not round_ or round_.round_id != draft.round_id:
            # A card from an archived/previous round is server-side stale. It
            # must not become a read-only card of the newly active round.
            self._discard_draft(telegram_id)
            return None
        if self.now() >= round_.deadline_msk:
            draft.phase = "L0"
            self._save_draft(telegram_id)
            return "deadline"
        return action

    def _touch(self, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if draft:
            draft.revision += 1
            self._save_draft(telegram_id)

    def _draft_snapshot(self, telegram_id: str, draft: Draft) -> dict:
        return {
            "version": 4, "draft_id": draft.draft_id, "participant_id": telegram_id,
            "chat_id": draft.chat_id, "round_id": draft.round_id, "state": "active",
            "revision": draft.revision, "active_message_id": draft.active_message_id,
            "reanchor_pending": draft.reanchor_pending,
            "reanchor_predecessor_id": draft.reanchor_predecessor_id,
            "structure": list(draft.structure),
            "bets": [{"type": bet.bet_type.value, "stake": bet.stake, "bet_id": bet.bet_id, "events": [self._event_snapshot(event) for event in bet.events]} for bet in draft.bets],
            "current_events": [self._event_snapshot(event) for event in draft.current_events],
            "selected_match_id": draft.selected_match_id, "phase": draft.phase,
            "expresses": [[self._event_snapshot(event) for event in express] for express in draft.expresses],
            "express_index": draft.express_index, "event_page": draft.event_page,
            "match_page": draft.match_page, "replacement": draft.replacement,
            "replacement_kind": draft.replacement_kind, "selection_slots": draft.selection_slots,
            "return_phase": draft.return_phase, "pending_reset": draft.pending_reset,
            "stake_edit": draft.stake_edit, "stake_edit_index": draft.stake_edit_index,
        }

    def _draft_from_snapshot(self, value: dict) -> Draft:
        def event(item: dict) -> BetEvent:
            return BetEvent(item["match_id"], Market(item["market"]), Decimal(item["odds"]), Decimal(item["total_line"]) if item["total_line"] is not None else None)
        bets = [DraftBet(BetType(item["type"]), [event(part) for part in item["events"]], item["stake"], item.get("bet_id", "")) for item in value["bets"]]
        draft = Draft(
            round_id=value["round_id"], structure=tuple(value["structure"]), bets=bets,
            current_events=[event(item) for item in value["current_events"]],
            selected_match_id=value["selected_match_id"], phase=value["phase"], draft_id=value["draft_id"],
            revision=value["revision"], chat_id=value["chat_id"], active_message_id=value["active_message_id"],
            reanchor_pending=bool(value.get("reanchor_pending", False)),
            reanchor_predecessor_id=value.get("reanchor_predecessor_id"),
            expresses=[[event(item) for item in express] for express in value["expresses"]],
            express_index=value["express_index"], event_page=value["event_page"], match_page=value["match_page"],
            replacement=value["replacement"], replacement_kind=value.get("replacement_kind", "full" if value["replacement"] else "new"),
            selection_slots=dict(value.get("selection_slots", {})), return_phase=value["return_phase"],
            pending_reset=value.get("pending_reset"), stake_edit=value.get("stake_edit"),
            stake_edit_index=value.get("stake_edit_index", 0),
        )
        self._ensure_identities(draft)
        return draft

    @staticmethod
    def _new_identity(prefix: str) -> str:
        return f"{prefix}{secrets.token_hex(4)}"

    def _ensure_identities(self, draft: Draft) -> None:
        for event in draft.current_events:
            draft.selection_slots.setdefault(event.match_id, self._new_identity("s"))
        for bet in draft.bets:
            if not bet.bet_id:
                bet.bet_id = self._new_identity("b")

    def _begin_prediction(self, chat_id: int, telegram_id: str) -> None:
        round_ = self.repository.get_active_round()
        if not round_:
            self.telegram.send_message(chat_id, "Сейчас нет открытого тура.")
            return
        draft = self._draft(telegram_id)
        if draft and (draft.round_id != round_.round_id or draft.chat_id != chat_id):
            self._discard_draft(telegram_id)
            draft = None
        if draft:
            if self.now() >= round_.deadline_msk:
                self._deadline_screen(chat_id, telegram_id)
            else:
                self._reanchor_current(chat_id, telegram_id, "predict_reanchor")
            return
        participant = self.repository.get_participant(telegram_id)
        if participant and self.repository.get_prediction(round_.round_id, participant.participant_id):
            self._show_my(chat_id, telegram_id)
            return
        if self.now() >= round_.deadline_msk:
            self.telegram.send_message(chat_id, "Дедлайн тура уже прошёл: новый прогноз и замена закрыты.")
            return
        self.drafts[telegram_id] = Draft(round_id=round_.round_id, draft_id=secrets.token_hex(4), chat_id=chat_id)
        self._save_draft(telegram_id)
        self._render_matches(chat_id, telegram_id)

    def _start_replacement(self, chat_id: int, telegram_id: str) -> None:
        existing_draft = self._draft(telegram_id)
        active = self.repository.get_active_round()
        if existing_draft and (not active or existing_draft.round_id != active.round_id or existing_draft.chat_id != chat_id):
            self._discard_draft(telegram_id)
            existing_draft = None
        if existing_draft:
            self._render_current(chat_id, telegram_id)
            return
        round_ = self.repository.get_active_round()
        if not round_ or self.now() >= round_.deadline_msk:
            self.telegram.send_message(chat_id, "Дедлайн тура уже прошёл: замена закрыта.")
            return
        self.drafts[telegram_id] = Draft(round_id=round_.round_id, draft_id=secrets.token_hex(4), chat_id=chat_id, replacement=True, replacement_kind="full")
        self._save_draft(telegram_id)
        self._render_matches(chat_id, telegram_id)

    def _render_full_replace_confirmation(self, chat_id: int, telegram_id: str) -> None:
        """R0 is intentionally not a draft: decline never creates state."""
        round_ = self.repository.get_active_round()
        if not round_ or self.now() >= round_.deadline_msk:
            self.telegram.send_message(chat_id, "Дедлайн тура уже прошёл: замена закрыта.")
            return
        self.telegram.send_message(
            chat_id,
            "🔄 Заменить прогноз?\nВы начнёте с пустого черновика. Текущий прогноз останется действующим до confirm.",
            _keyboard([("📝 Начать полную замену", "full:start"), ("← Мой прогноз", "full:back")]),
        )

    def _clone_confirmed_draft(self, prediction: Prediction, chat_id: int) -> Draft:
        selected: list[BetEvent] = []
        seen: set[str] = set()
        for bet in prediction.bets:
            for event in bet.events:
                if event.match_id not in seen:
                    selected.append(event)
                    seen.add(event.match_id)
        expresses = [[event for event in bet.events] for bet in prediction.bets if bet.bet_type == BetType.EXPRESS]
        structure = (sum(bet.bet_type == BetType.SINGLE for bet in prediction.bets), sum(bet.bet_type == BetType.EXPRESS for bet in prediction.bets))
        return Draft(
            round_id=prediction.round_id, structure=structure,
            bets=[DraftBet(bet.bet_type, list(bet.events), bet.stake, self._new_identity("b")) for bet in prediction.bets],
            current_events=selected, phase="R1", draft_id=secrets.token_hex(4), chat_id=chat_id,
            expresses=expresses, replacement=True, replacement_kind="correction",
            selection_slots={event.match_id: self._new_identity("s") for event in selected},
        )

    def _start_correction(self, chat_id: int, telegram_id: str) -> None:
        round_ = self.repository.get_active_round()
        if not round_ or self.now() >= round_.deadline_msk:
            self.telegram.send_message(chat_id, "Дедлайн тура уже прошёл: корректировки закрыты.")
            return
        existing = self._draft(telegram_id)
        if existing and existing.round_id == round_.round_id and existing.chat_id == chat_id:
            self._render_current(chat_id, telegram_id)
            return
        if existing:
            self._discard_draft(telegram_id)
        participant = self.repository.get_participant(telegram_id)
        prediction = self.repository.get_prediction(round_.round_id, participant.participant_id) if participant else None
        if not prediction:
            self._begin_prediction(chat_id, telegram_id)
            return
        # One save is the clone's commit point: confirmed repository state is
        # untouched until the ordinary final confirm path succeeds.
        self.drafts[telegram_id] = self._clone_confirmed_draft(prediction, chat_id)
        self._save_draft(telegram_id)
        self._render_correction_hub(chat_id, telegram_id)

    def _correction_route(self, chat_id: int, telegram_id: str, phase: str) -> None:
        draft = self._draft(telegram_id)
        if not draft or draft.replacement_kind != "correction":
            self._show_my(chat_id, telegram_id)
            return
        if phase == "M1":
            draft.return_phase = draft.phase
        draft.phase = phase
        self._touch(telegram_id)
        self._render_current(chat_id, telegram_id)

    def _render_correction_hub(self, chat_id: int, telegram_id: str) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_:
            return
        draft.phase = "R1"
        self._draft_screen(
            chat_id, telegram_id,
            f"✏️ Корректировки прогноза\n🏟️ Тур: {round_.round_id}\n⏰ Дедлайн: {round_.deadline_msk:%d.%m %H:%M}\nТекущий подтверждённый прогноз остаётся действующим до подтверждения корректировок.",
            [("⚽ Изменить события", "r:events"), ("🎯 Пересобрать экспрессы", "r:expresses"), ("💰 Изменить суммы", "r:amounts"), ("← Мой прогноз", "r:my"), ("✖️ Отменить корректировки", "cancel")],
        )

    def _resume_draft(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if draft and self._deadline_guard(chat_id, telegram_id, draft):
            return
        if draft and draft.phase in {"C0", "M1"}:
            draft.phase = draft.return_phase
            self._touch(telegram_id)
        if draft:
            self._reanchor_current(chat_id, telegram_id, "resume_reanchor")
        else:
            self._menu(chat_id, "Черновик не найден.")

    def _deadline_guard(self, chat_id: int, telegram_id: str, draft: Draft | None = None) -> bool:
        draft = draft or self._draft(telegram_id)
        round_ = self.repository.get_active_round()
        if not draft or not round_ or draft.round_id != round_.round_id or self.now() < round_.deadline_msk:
            return False
        self._deadline_screen(chat_id, telegram_id)
        return True

    def _reanchor_current(self, chat_id: int, telegram_id: str, action: str) -> None:
        draft = self._draft(telegram_id)
        if not draft:
            self._menu(chat_id, "Черновик не найден.")
            return
        if self._deadline_guard(chat_id, telegram_id, draft):
            return
        marker = _reanchor_context.set((draft.draft_id, action, draft.phase))
        try:
            self._render_current(chat_id, telegram_id)
        finally:
            _reanchor_context.reset(marker)

    @staticmethod
    def _draft_progress(draft: Draft) -> str:
        phase = {
            "E1": "выбор матчей",
            "E2": "выбор события",
            "E3": "проверка выбранных событий",
            "X0": "выбор схемы экспрессов",
            "X1": "сборка экспресса",
            "X2": "проверка экспрессов",
            "B1": "распределение сумм",
            "B2": "проверка новых сумм",
            "W1": "подтверждение изменения",
            "P1": "предпросмотр",
            "C0": "подтверждение отмены",
        }.get(draft.phase, "продолжение черновика")
        placed = len([bet for bet in draft.bets if bet.stake is not None])
        return f"Этап: {phase}.\nСобытия: {len(draft.current_events)} из 6–9 · суммы: {placed}/5."

    def _draft_token_matches(self, telegram_id: str, chat_id: int, draft_id: str, revision: str) -> Draft | None:
        draft = self._draft(telegram_id)
        try:
            revision_value = int(revision)
        except ValueError:
            return None
        if not draft or (draft.draft_id, draft.revision, draft.chat_id) != (draft_id, revision_value, chat_id):
            return None
        return draft

    def _resume_from_my(self, chat_id: int, telegram_id: str, data: str) -> None:
        try:
            _, _, draft_id, revision = data.split(":", 3)
        except ValueError:
            return
        if not self._draft_token_matches(telegram_id, chat_id, draft_id, revision):
            self.telegram.send_message(chat_id, "Черновик уже изменился. Откройте /my.")
            return
        if self._deadline_guard(chat_id, telegram_id):
            return
        self._resume_draft(chat_id, telegram_id)

    def _request_draft_restart(self, chat_id: int, telegram_id: str, data: str) -> None:
        try:
            _, _, _, draft_id, revision = data.split(":", 4)
        except ValueError:
            return
        draft = self._draft_token_matches(telegram_id, chat_id, draft_id, revision)
        if not draft:
            self.telegram.send_message(chat_id, "Черновик уже изменился. Откройте /my.")
            return
        if self._deadline_guard(chat_id, telegram_id, draft):
            return
        self._trace_draft(telegram_id, "restart_requested", draft, draft.phase, draft.active_message_id, draft.active_message_id, "awaiting_confirmation")
        self.telegram.send_message(
            chat_id,
            "Начать заново? Незавершённый черновик будет удалён только после подтверждения.",
            _keyboard([
                ("🗑 Удалить и начать заново", f"draft:restart:yes:{draft.draft_id}:{draft.revision}"),
                ("← Оставить черновик", f"draft:restart:no:{draft.draft_id}:{draft.revision}"),
            ]),
        )

    def _confirm_draft_restart(self, chat_id: int, telegram_id: str, data: str) -> None:
        try:
            _, _, _, draft_id, revision = data.split(":", 4)
        except ValueError:
            return
        draft = self._draft_token_matches(telegram_id, chat_id, draft_id, revision)
        if not draft:
            self.telegram.send_message(chat_id, "Черновик уже изменился. Откройте /my.")
            return
        if self._deadline_guard(chat_id, telegram_id, draft):
            return
        phase_before, card_from = draft.phase, draft.active_message_id
        if card_from is not None:
            self.telegram.clear_keyboard(chat_id, card_from)
        self._discard_draft(telegram_id)
        self._begin_prediction(chat_id, telegram_id)
        self._trace_draft(telegram_id, "restart_confirmed", draft, phase_before, card_from, None, "success")

    def _decline_draft_restart(self, chat_id: int, telegram_id: str, data: str) -> None:
        try:
            _, _, _, draft_id, revision = data.split(":", 4)
        except ValueError:
            return
        if not self._draft_token_matches(telegram_id, chat_id, draft_id, revision):
            self.telegram.send_message(chat_id, "Черновик уже изменился. Откройте /my.")
            return
        if self._deadline_guard(chat_id, telegram_id):
            return
        self._show_my(chat_id, telegram_id)

    def _render_current(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft:
            self._begin_prediction(chat_id, telegram_id)
            return
        if draft.phase == "L0": self._deadline_screen(chat_id, telegram_id)
        elif draft.phase == "E1": self._render_matches(chat_id, telegram_id)
        elif draft.phase == "E2": self._select_match(chat_id, telegram_id, draft.selected_match_id or "")
        elif draft.phase == "E3": self._finish_bet(chat_id, telegram_id, render_only=True)
        elif draft.phase == "X0": self._render_scheme(chat_id, telegram_id)
        elif draft.phase == "X1": self._render_express(chat_id, telegram_id)
        elif draft.phase == "X2": self._render_express_review(chat_id, telegram_id)
        elif draft.phase == "B1": self._render_stake(chat_id, telegram_id)
        elif draft.phase == "B2": self._render_stake_review(chat_id, telegram_id)
        elif draft.phase == "W1": self._render_warning(chat_id, telegram_id)
        elif draft.phase == "P1": self._preview(chat_id, telegram_id)
        elif draft.phase == "M1": self._show_my(chat_id, telegram_id)
        elif draft.phase == "R1": self._render_correction_hub(chat_id, telegram_id)
        elif draft.phase == "C0": self._cancel_draft(chat_id, telegram_id)

    def _fixture(self, round_: Round, match_id: str):
        return next((item for item in round_.fixtures if item.match_id == match_id), None)

    def _event_label(self, round_: Round, event: BetEvent) -> str:
        fixture = self._fixture(round_, event.match_id)
        name = f"{fixture.home_team} — {fixture.away_team}" if fixture else event.match_id
        market = f"{event.market.value} {event.total_line_snapshot}" if event.total_line_snapshot is not None else event.market.value
        return f"{name} · {market} · {event.odds_snapshot}"

    @staticmethod
    def _match_label(fixture, selected: bool) -> str:
        return compact_match_label(fixture, selected)

    @staticmethod
    def _match_pages(round_: Round) -> list[list]:
        page_size = 6 if len(round_.fixtures) <= 12 else 7
        return [page for page in (round_.fixtures[:page_size], round_.fixtures[page_size:]) if page]

    def _match_page_boundary_ack(self, telegram_id: str, action: str) -> str | None:
        if not action.startswith("page:m:"):
            return None
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_:
            return None
        pages = self._match_pages(round_)
        delta = int(action.rsplit(":", 1)[-1])
        next_page = max(0, min(len(pages) - 1, draft.match_page + delta))
        if next_page != draft.match_page:
            return None
        if delta < 0:
            return f"Вы уже на стр. 1/{len(pages)}."
        selected = {event.match_id for event in draft.current_events}
        if pages and any(fixture.match_id not in selected for fixture in pages[0]):
            return "Это последняя страница. Незанятые матчи — на стр. 1."
        return f"Вы уже на стр. {draft.match_page + 1}/{len(pages)}."

    def _render_matches(self, chat_id: int, telegram_id: str) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_: return
        draft.phase, draft.selected_match_id = "E1", None
        selected = {event.match_id for event in draft.current_events}
        pages = self._match_pages(round_)
        draft.match_page = min(draft.match_page, len(pages) - 1)
        page = pages[draft.match_page]
        buttons = [[(self._match_label(item, item.match_id in selected), f"match:{item.match_id}")] for item in page]
        complete = ("✅ Завершить", "finish-events") if len(draft.current_events) >= 6 else ("✅ 6+ событий", "need6")
        buttons.append([("◀️", "page:m:-1"), ("▶️", "page:m:+1"), complete, ("✖️", "cancel")])
        chosen = "\n".join(f"• {self._event_label(round_, item)}" for item in draft.current_events)
        self._draft_screen(chat_id, telegram_id, f"📝 Прогноз\n🏟️ Тур: {round_.round_id}\n⏰ Дедлайн: {round_.deadline_msk:%d.%m %H:%M}\nВыбрано событий: {len(draft.current_events)} из 6–9\n{chosen}\nВыберите матч · {draft.match_page + 1}/{len(pages)}:", buttons)

    def _select_match(self, chat_id: int, telegram_id: str, match_id: str) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_: return
        if match_id.startswith("page:m:"):
            last_page = len(self._match_pages(round_)) - 1
            next_page = max(0, min(last_page, draft.match_page + int(match_id.rsplit(":", 1)[-1])))
            if next_page == draft.match_page:
                return
            draft.match_page = next_page; self._touch(telegram_id)
            self._render_matches(chat_id, telegram_id); return
        fixture = self._fixture(round_, match_id)
        if not fixture: return
        if match_id not in {event.match_id for event in draft.current_events} and len(draft.current_events) >= 9:
            self.telegram.send_message(chat_id, "Уже выбрано 9 событий. Завершите выбор или замените одно из выбранных.")
            self._render_matches(chat_id, telegram_id); return
        draft.selected_match_id, draft.phase, draft.event_page = match_id, "E2", 0
        self._touch(telegram_id)
        self._render_event_choices(chat_id, telegram_id)

    def _render_event_choices(self, chat_id: int, telegram_id: str) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_ or not draft.selected_match_id: return
        fixture = self._fixture(round_, draft.selected_match_id)
        if not fixture: return
        markets = list(fixture.odds.items())
        buttons = []
        for market, odds in markets:
            label = f"{market.value}{' ' + str(fixture.total_line) if market in {Market.TB, Market.TM} else ''} · {odds}"
            buttons.append((label, f"market:{fixture.match_id}:{market.value}"))
        buttons = [buttons[index:index + 2] for index in range(0, len(buttons), 2)]
        current = next((event for event in draft.current_events if event.match_id == fixture.match_id), None)
        if current:
            buttons.append([("🗑 Убрать матч", f"remove:{fixture.match_id}")])
        current_line = f"\nТекущее событие: {self._event_label(round_, current)}" if current else ""
        self._draft_screen(chat_id, telegram_id, f"⚽ {fixture.home_team} — {fixture.away_team}{current_line}\nВыберите событие:", buttons, back=True, cancel=True)

    def _select_market(self, chat_id: int, telegram_id: str, match_id: str, market: Market) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_: return
        if match_id.startswith("page:e:"):
            draft.event_page = max(0, draft.event_page + int(match_id.rsplit(":", 1)[-1])); self._touch(telegram_id); self._render_event_choices(chat_id, telegram_id); return
        fixture = self._fixture(round_, match_id)
        if not fixture or draft.selected_match_id != match_id or market not in fixture.odds: return
        replacement = BetEvent(match_id, market, fixture.odds[market], fixture.total_line if market in {Market.TB, Market.TM} else None)
        old = list(draft.current_events)
        existing = next((item for item in draft.current_events if item.match_id == match_id), None)
        candidate = old + [replacement] if existing is None else (old if existing == replacement else [replacement if item.match_id == match_id else item for item in draft.current_events])
        # Replacing a market for the same selected match is compatible: slots,
        # bet identities and their stakes stay intact; only odds/payout change.
        if old != candidate and existing is not None:
            draft.current_events = candidate
            self._replace_event_references(draft, replacement)
            draft.phase, draft.selected_match_id = "E1", None
            self._touch(telegram_id); self._render_matches(chat_id, telegram_id)
            return
        if old != candidate and self._has_active_stakes(draft):
            self._warn_reset(chat_id, telegram_id, {"kind": "event", "match_id": match_id, "market": market.value}, "E2")
            return
        draft.current_events = candidate
        draft.selection_slots.setdefault(match_id, self._new_identity("s"))
        if old != candidate:
            draft.expresses, draft.express_index, draft.bets = [], 0, []
        draft.phase, draft.selected_match_id = "E1", None
        self._touch(telegram_id); self._render_matches(chat_id, telegram_id)

    @staticmethod
    def _replace_event_references(draft: Draft, replacement: BetEvent) -> None:
        def replace(items: list[BetEvent]) -> list[BetEvent]:
            return [replacement if item.match_id == replacement.match_id else item for item in items]
        draft.expresses = [replace(express) for express in draft.expresses]
        for bet in draft.bets:
            bet.events = replace(bet.events)

    def _remove_selected_match(self, chat_id: int, telegram_id: str, match_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft or draft.selected_match_id != match_id or not any(event.match_id == match_id for event in draft.current_events):
            return
        if len(draft.current_events) <= 1:
            self.telegram.send_message(chat_id, "В черновике должно остаться хотя бы одно событие.")
            self._render_event_choices(chat_id, telegram_id)
            return
        if draft.expresses or draft.bets:
            self._warn_reset(chat_id, telegram_id, {"kind": "remove", "match_id": match_id}, "E2")
            return
        self._remove_selection(draft, match_id)
        draft.phase, draft.selected_match_id = "E1", None
        self._touch(telegram_id); self._render_matches(chat_id, telegram_id)

    @staticmethod
    def _remove_selection(draft: Draft, match_id: str) -> None:
        draft.current_events = [event for event in draft.current_events if event.match_id != match_id]
        draft.selection_slots.pop(match_id, None)

    def _finish_bet(self, chat_id: int, telegram_id: str, render_only: bool = False) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_: return
        count = len(draft.current_events)
        if count < 6:
            self.telegram.send_message(chat_id, f"Выберите ещё {6-count} событие(я).")
            self._render_matches(chat_id, telegram_id); return
        if not render_only:
            draft.phase = "E3"
            self._touch(telegram_id)
        self._draft_screen(chat_id, telegram_id, f"✅ Выбрано событий: {count}\n" + "\n".join(f"• {self._event_label(round_, item)}" for item in draft.current_events) + "\nПерейти к сборке экспрессов?", [("🎯 Собрать экспрессы", "to-express"), ("← Продолжить выбор", "back")], cancel=True)

    def _enter_express(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft:
            return
        if draft.expresses and self._composition_valid(draft):
            draft.phase = "X2"
        elif draft.expresses:
            draft.phase = "X1"
        elif len(draft.current_events) == 7:
            draft.phase = "X0"
        else:
            draft.structure, draft.expresses, draft.express_index, draft.phase = self._schema_for_count(len(draft.current_events)), [], 0, "X1"
        self._touch(telegram_id)
        self._render_current(chat_id, telegram_id)

    @staticmethod
    def _schema_for_count(count: int) -> tuple[int, int]:
        return {6: (4, 1), 8: (3, 2), 9: (3, 2)}[count]

    def _render_scheme(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft: return
        draft.phase = "X0"
        self._draft_screen(chat_id, telegram_id, "🎯 Как собрать 7 событий?", [("4+1 · экспресс из 3", "schema:41"), ("3+2 · два экспресса по 2", "schema:32")], back=True, cancel=True)

    def _set_structure(self, chat_id: int, telegram_id: str, data: str) -> None:
        draft = self._draft(telegram_id)
        if not draft: return
        target = (4, 1) if data in {"schema:41", "structure:4+1"} else (3, 2)
        changed = target != draft.structure
        if changed and self._has_active_stakes(draft):
            self._warn_reset(chat_id, telegram_id, {"kind": "schema", "structure": list(target)}, "X0")
            return
        draft.structure = target
        draft.expresses, draft.express_index, draft.phase = [], 0, "X1"
        if changed:
            draft.bets = []
        self._touch(telegram_id); self._render_express(chat_id, telegram_id)

    def _express_sizes(self, draft: Draft) -> list[int]:
        count = len(draft.current_events)
        if draft.structure == (4, 1): return [2 if count == 6 else 3]
        return {7: [2, 2], 8: [2, 3], 9: [3, 3]}.get(count, [])

    def _render_express(self, chat_id: int, telegram_id: str) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_: return
        sizes = self._express_sizes(draft)
        if draft.express_index >= len(sizes): draft.phase = "X2"; self._render_express_review(chat_id, telegram_id); return
        while len(draft.expresses) <= draft.express_index: draft.expresses.append([])
        selected = draft.expresses[draft.express_index]
        occupied = {item.match_id for index, express in enumerate(draft.expresses) if index != draft.express_index for item in express}
        buttons = []
        for item in draft.current_events:
            if item.match_id in {event.match_id for event in selected}:
                label, action = f"✅ {self._event_label(round_, item)}", f"x:t:{item.match_id}"
            elif item.match_id in occupied:
                label, action = f"🔒 {self._event_label(round_, item)}", f"x:t:{item.match_id}"
            else:
                label, action = f"▫️ {self._event_label(round_, item)}", f"x:t:{item.match_id}"
            buttons.append((label, action))
        if len(selected) == sizes[draft.express_index]: buttons.append(("✅ Экспресс готов", "x:done"))
        text = f"🎯 Экспресс {draft.express_index+1} из {len(sizes)}\nНужно событий: {sizes[draft.express_index]}\n" + "\n".join(f"• {self._event_label(round_, item)}" for item in selected)
        self._draft_screen(chat_id, telegram_id, text, buttons, back=True, cancel=True)

    def _express_action(self, chat_id: int, telegram_id: str, data: str) -> None:
        draft = self._draft(telegram_id)
        if not draft: return
        if data.startswith("x:p:"):
            return
        if data == "x:done":
            if len(draft.expresses[draft.express_index]) != self._express_sizes(draft)[draft.express_index]: return
            draft.express_index += 1; draft.event_page = 0; draft.phase = "X1" if draft.express_index < len(self._express_sizes(draft)) else "X2"
            self._touch(telegram_id); self._render_current(chat_id, telegram_id); return
        if data.startswith("x:t:"):
            match_id = data.rsplit(":", 1)[-1]
            current = draft.expresses[draft.express_index]
            before = list(current)
            if any(item.match_id == match_id for item in current): current[:] = [item for item in current if item.match_id != match_id]
            elif len(current) < self._express_sizes(draft)[draft.express_index]:
                event = next((item for item in draft.current_events if item.match_id == match_id), None)
                if event and not any(match_id == item.match_id for index, express in enumerate(draft.expresses) if index != draft.express_index for item in express): current.append(event)
            if current != before:
                self._touch(telegram_id)
            self._render_express(chat_id, telegram_id)
        elif data == "x:rebuild":
            draft.expresses, draft.express_index, draft.phase = [], 0, "X0" if len(draft.current_events) == 7 else "X1"; self._touch(telegram_id); self._render_current(chat_id, telegram_id)
        elif data.startswith("x:reset:"):
            index = int(data.rsplit(":", 1)[-1]); draft.expresses[index] = []; draft.express_index, draft.phase = index, "X1"; self._touch(telegram_id); self._render_express(chat_id, telegram_id)
        elif data == "x:schema":
            draft.phase = "X0"; self._touch(telegram_id); self._render_scheme(chat_id, telegram_id)

    def _warn_reset(self, chat_id: int, telegram_id: str, change: dict, return_phase: str) -> None:
        draft = self._draft(telegram_id)
        if not draft:
            return
        draft.pending_reset = dict(change, return_phase=return_phase)
        draft.phase = "W1"
        self._touch(telegram_id)
        self._render_warning(chat_id, telegram_id)

    def _render_warning(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft or not draft.pending_reset:
            return
        if draft.pending_reset.get("kind") == "remove":
            match_id = str(draft.pending_reset.get("match_id", ""))
            round_ = self.repository.get_active_round()
            event = next((item for item in draft.current_events if item.match_id == match_id), None)
            label = self._event_label(round_, event) if round_ and event else "выбранное событие"
            text = (
                "⚠️ У этого матча уже есть связанные экспрессы или суммы.\n"
                f"{label}\nСохранится: остальные события и максимально совместимые ставки/суммы.\n"
                "Будет очищено: ставка удалённого матча и несовместимые ставки/суммы."
            )
            button = "✅ Убрать матч"
        else:
            description = "изменение выбранного события" if draft.pending_reset.get("kind") == "event" else "смена схемы экспрессов"
            text = f"⚠️ Это изменение несовместимо с текущими суммами.\n{description}.\nСуммы будут сброшены только после подтверждения."
            button = "✅ Продолжить и сбросить суммы"
        self._draft_screen(chat_id, telegram_id, text, [(button, "warn:apply"), ("← Оставить без изменений", "warn:cancel")])

    def _apply_pending_reset(self, chat_id: int, telegram_id: str) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_ or not draft.pending_reset:
            return
        change = draft.pending_reset
        if change.get("kind") == "event":
            fixture = self._fixture(round_, str(change.get("match_id", "")))
            try:
                market = Market(str(change.get("market", "")))
            except ValueError:
                return
            if not fixture or market not in fixture.odds:
                return
            event = BetEvent(fixture.match_id, market, fixture.odds[market], fixture.total_line if market in {Market.TB, Market.TM} else None)
            draft.current_events = [event if item.match_id == fixture.match_id else item for item in draft.current_events]
            draft.expresses, draft.express_index, draft.bets, draft.phase = [], 0, [], "E1"
        elif change.get("kind") == "schema":
            target = tuple(change.get("structure", ()))
            if target not in {(4, 1), (3, 2)}:
                return
            draft.structure, draft.expresses, draft.express_index, draft.bets, draft.phase = target, [], 0, [], "X1"
        elif change.get("kind") == "remove":
            match_id = str(change.get("match_id", ""))
            if not any(event.match_id == match_id for event in draft.current_events):
                return
            self._reconcile_removed_match(draft, match_id)
        else:
            return
        draft.stake_edit, draft.stake_edit_index, draft.pending_reset = None, 0, None
        self._touch(telegram_id); self._render_current(chat_id, telegram_id)

    @staticmethod
    def _schema_options(count: int) -> list[tuple[tuple[int, int], tuple[int, ...]]]:
        return {
            6: [((4, 1), (2,))],
            7: [((4, 1), (3,)), ((3, 2), (2, 2))],
            8: [((3, 2), (2, 3))],
            9: [((3, 2), (3, 3))],
        }.get(count, [])

    def _reconcile_removed_match(self, draft: Draft, match_id: str) -> None:
        """Apply §5.1 in one in-memory mutation before the single durable save."""
        previous = list(draft.bets)
        self._remove_selection(draft, match_id)
        count = len(draft.current_events)
        if count < 6:
            # The remaining selections are still valuable, but no valid coupon
            # exists yet; bets are intentionally rebuilt only after N reaches 6.
            draft.expresses, draft.express_index, draft.bets, draft.phase = [], 0, [], "E1"
            return

        candidates: list[tuple[int, list[BetEvent]]] = []
        for index, bet in enumerate(previous):
            if bet.bet_type != BetType.EXPRESS:
                continue
            residual = [event for event in bet.events if event.match_id != match_id]
            if len(residual) in {2, 3}:
                candidates.append((index, residual))

        best: tuple[int, tuple[int, int], list[list[BetEvent]]] | None = None
        direct_single_ids = {bet.events[0].match_id for bet in previous if bet.bet_type == BetType.SINGLE and len(bet.events) == 1 and bet.events[0].match_id != match_id}
        for structure, sizes in self._schema_options(count):
            for chosen in permutations(candidates, len(sizes)):
                if tuple(sorted(len(events) for _, events in chosen)) != tuple(sorted(sizes)):
                    continue
                groups = [events for _, events in sorted(chosen, key=lambda item: item[0])]
                used = {event.match_id for group in groups for event in group}
                if len(used) != sum(sizes):
                    continue
                score = len(chosen) + sum(event.match_id in direct_single_ids for event in draft.current_events if event.match_id not in used)
                # Keep the already chosen schema where preservation is tied.
                if best is None or score > best[0] or (score == best[0] and structure == draft.structure and best[1] != draft.structure):
                    best = (score, structure, groups)
        if best is None:
            draft.expresses, draft.express_index, draft.bets, draft.phase = [], 0, [], "X0" if count == 7 else "X1"
            return
        _score, structure, groups = best
        draft.structure, draft.expresses, draft.express_index = structure, groups, 0
        draft.bets = self._build_bets(draft, previous)
        # An express that merely lost this selected leg retains its identity
        # and amount when the residual two/three legs fit the new schema.
        for bet in draft.bets:
            if bet.bet_type != BetType.EXPRESS:
                continue
            residual_signature = tuple(event.match_id for event in bet.events)
            source = next(
                (
                    old for old in previous
                    if old.bet_type == BetType.EXPRESS
                    and tuple(event.match_id for event in old.events if event.match_id != match_id) == residual_signature
                ),
                None,
            )
            if source:
                bet.bet_id, bet.stake = source.bet_id or self._new_identity("b"), source.stake
        if not self._composition_valid(draft):
            draft.phase = "X1"
        elif not self._has_active_stakes(draft):
            draft.phase = "B1"
        else:
            draft.phase = "P1"

    def _cancel_pending_reset(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft or not draft.pending_reset:
            return
        draft.phase = str(draft.pending_reset.get("return_phase", "E1"))
        draft.pending_reset = None
        self._touch(telegram_id); self._render_current(chat_id, telegram_id)

    def _composition_valid(self, draft: Draft) -> bool:
        sizes = self._express_sizes(draft)
        return len(draft.expresses) == len(sizes) and all(len(item) == size for item, size in zip(draft.expresses, sizes)) and len({event.match_id for express in draft.expresses for event in express}) == sum(sizes)

    def _build_bets(self, draft: Draft, previous: list[DraftBet] | None = None) -> list[DraftBet]:
        express_ids = {event.match_id for express in draft.expresses for event in express}
        desired = [DraftBet(BetType.SINGLE, [event]) for event in draft.current_events if event.match_id not in express_ids]
        desired += [DraftBet(BetType.EXPRESS, list(events)) for events in draft.expresses]
        old = previous if previous is not None else draft.bets
        by_signature: dict[tuple[BetType, tuple[str, ...]], list[DraftBet]] = {}
        for bet in old:
            signature = (bet.bet_type, tuple(event.match_id for event in bet.events))
            by_signature.setdefault(signature, []).append(bet)
        for bet in desired:
            signature = (bet.bet_type, tuple(event.match_id for event in bet.events))
            compatible = by_signature.get(signature, [])
            if compatible:
                old_bet = compatible.pop(0)
                bet.bet_id, bet.stake = old_bet.bet_id or self._new_identity("b"), old_bet.stake
            else:
                bet.bet_id = self._new_identity("b")
        return desired

    def _render_express_review(self, chat_id: int, telegram_id: str) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_: return
        if not self._composition_valid(draft): draft.phase = "X1"; self._render_express(chat_id, telegram_id); return
        draft.phase, draft.bets = "X2", self._build_bets(draft)
        lines = ["🎯 Экспрессы собраны"]
        for index, express in enumerate(draft.expresses, 1): lines.append(f"Экспресс {index}: " + "; ".join(self._event_label(round_, item) for item in express))
        buttons = [("💰 Распределить банк", "bank"), ("🔄 Пересобрать экспрессы", "x:rebuild")]
        if len(draft.current_events) == 7:
            buttons.append(("↔️ Сменить схему", "x:schema"))
        buttons += [(f"🗑 Сбросить экспресс {index+1}", f"x:reset:{index}") for index in range(len(draft.expresses))]
        self._draft_screen(chat_id, telegram_id, "\n".join(lines), buttons, back=True, cancel=True)

    def _reset_stakes(self, draft: Draft) -> None:
        if any(bet.stake is not None for bet in draft.bets):
            draft.bets = []

    def _reset_for_structure_change(self, draft: Draft) -> None:
        if draft.expresses or draft.bets:
            draft.expresses, draft.express_index, draft.bets = [], 0, []

    @staticmethod
    def _has_active_stakes(draft: Draft) -> bool:
        return len(draft.bets) == 5 and all(bet.stake is not None for bet in draft.bets)

    def _start_stake_entry(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft:
            return
        if self._has_active_stakes(draft):
            draft.stake_edit, draft.stake_edit_index = [None] * 5, 0
        self._touch(telegram_id)
        self._render_stake(chat_id, telegram_id)

    def _stake_target(self, draft: Draft) -> tuple[list[int | None], int, bool]:
        if draft.stake_edit is not None:
            return draft.stake_edit, draft.stake_edit_index, True
        return [bet.stake for bet in draft.bets], next((index for index, bet in enumerate(draft.bets) if bet.stake is None), len(draft.bets)), False

    @staticmethod
    def _stake_limits(values: list[int | None], index: int, bet_count: int) -> tuple[int, int, int]:
        remaining = BANK - sum(value or 0 for value in values)
        later = bet_count - index - 1
        minimum = max(MIN_STAKE, remaining - later * MAX_STAKE)
        maximum = min(MAX_STAKE, remaining - later * MIN_STAKE)
        return remaining, minimum, maximum

    def _render_stake(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft: return
        if not draft.bets: draft.bets = self._build_bets(draft)
        values, index, edit_mode = self._stake_target(draft)
        if index == len(draft.bets):
            if edit_mode:
                draft.phase = "B2"; self._render_stake_review(chat_id, telegram_id); return
            draft.phase = "P1"; self._preview(chat_id, telegram_id); return
        draft.phase = "B1"
        remaining, minimum, maximum = self._stake_limits(values, index, len(draft.bets))
        recommended = [minimum] if minimum == maximum else [value for value in (500, 750, 1000, 1500, 2500, 2000) if minimum <= value <= maximum]
        old_value = draft.bets[index].stake
        if minimum != maximum and edit_mode and old_value is not None and minimum <= old_value <= maximum:
            recommended = [old_value] + [value for value in recommended if value != old_value]
        recommended = sorted(recommended[:5])
        bet = draft.bets[index]
        name = "Экспресс" if bet.bet_type == BetType.EXPRESS else "Ординар"
        round_ = self.repository.get_active_round()
        description = "; ".join(self._event_label(round_, event) for event in bet.events) if round_ else name
        amount_buttons = [(f"✅ Выбрать {minimum:,}".replace(",", " "), f"stake:{minimum}")] if minimum == maximum else [(f"{value:,}".replace(",", " "), f"stake:{value}") for value in recommended]
        self._draft_screen(chat_id, telegram_id, f"💰 {name}: {description}\nТекущая сумма: {old_value if old_value is not None else '—'}\nОстаток: {remaining}\nДопустимо: {minimum}–{maximum}, шаг 50\nВыберите сумму или введите её сообщением:", amount_buttons, back=True, cancel=True)

    def _set_stake(self, chat_id: int, telegram_id: str, stake: int) -> None:
        draft = self._draft(telegram_id)
        if not draft or draft.phase != "B1": return
        values, index, edit_mode = self._stake_target(draft)
        if index == len(draft.bets): return
        _remaining, minimum, maximum = self._stake_limits(values, index, len(draft.bets))
        if not minimum <= stake <= maximum or stake % STAKE_STEP:
            message = f"Введите ровно {minimum} — это единственная допустимая сумма." if minimum == maximum else f"Введите сумму от {minimum} до {maximum}, кратную 50."
            self.telegram.send_message(chat_id, message); return
        if edit_mode:
            assert draft.stake_edit is not None
            draft.stake_edit[index] = stake; draft.stake_edit_index += 1
        else:
            draft.bets[index].stake = stake
        self._touch(telegram_id); self._render_stake(chat_id, telegram_id)

    def _render_stake_review(self, chat_id: int, telegram_id: str) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        if not draft or not round_ or draft.stake_edit is None:
            return
        lines = ["💰 Новое распределение"]
        for bet, stake in zip(draft.bets, draft.stake_edit):
            lines.append(f"• {'; '.join(self._event_label(round_, event) for event in bet.events)} — {stake}")
        lines.append(f"Итого: {sum(value or 0 for value in draft.stake_edit):,}".replace(",", " "))
        self._draft_screen(chat_id, telegram_id, "\n".join(lines), [("✅ Применить суммы", "stakes:apply"), ("← Изменить", "stakes:edit"), ("✖️ Отменить изменение", "stakes:cancel")])

    def _apply_stake_edit(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft or draft.stake_edit is None or len(draft.stake_edit) != 5 or any(value is None for value in draft.stake_edit) or sum(draft.stake_edit) != BANK:
            self.telegram.send_message(chat_id, "Новое распределение должно содержать пять сумм и итог 5 000."); return
        if any(not MIN_STAKE <= int(value) <= MAX_STAKE or int(value) % STAKE_STEP for value in draft.stake_edit):
            self.telegram.send_message(chat_id, "Введите суммы от 500 до 2 500 с шагом 50."); return
        for bet, value in zip(draft.bets, draft.stake_edit): bet.stake = value
        draft.stake_edit, draft.stake_edit_index, draft.phase = None, 0, "P1"
        self._touch(telegram_id); self._preview(chat_id, telegram_id)

    def _edit_stake_again(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft or draft.stake_edit is None: return
        draft.stake_edit_index = 4; draft.stake_edit[4] = None; draft.phase = "B1"
        self._touch(telegram_id); self._render_stake(chat_id, telegram_id)

    def _cancel_stake_edit(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft: return
        draft.stake_edit, draft.stake_edit_index, draft.phase = None, 0, "P1"
        self._touch(telegram_id); self._preview(chat_id, telegram_id)

    def _preview(self, chat_id: int, telegram_id: str) -> None:
        draft, round_ = self._draft(telegram_id), self.repository.get_active_round()
        prediction = self._prediction_from_draft(telegram_id)
        if not draft or not round_ or not prediction: return
        draft.phase = "P1"
        lines = ["🔎 Проверьте прогноз", f"\n🏟️ Тур: {round_.round_id}", f"⏰ Дедлайн: {round_.deadline_msk:%d.%m %H:%M}"]
        for bet in prediction.bets:
            odds = _combined_odds(bet); title = "ОРДИНАР" if bet.bet_type == BetType.SINGLE else "ЭКСПРЕСС"
            lines += [f"\n{title}"] + [f"⚽ {self._event_label(round_, item)}" for item in bet.events] + [f"💵 {bet.stake} × {odds} = {(Decimal(bet.stake)*odds).quantize(Decimal('1'), rounding=ROUND_HALF_UP)}"]
        self._draft_screen(chat_id, telegram_id, "\n".join(lines), [("✅ Подтвердить", "confirm"), ("💰 Изменить суммы", "bank"), ("🔄 Пересобрать экспрессы", "x:rebuild")], back=True, cancel=True)

    def _confirm(self, chat_id: int, telegram_id: str) -> None:
        draft, prediction, round_ = self._draft(telegram_id), self._prediction_from_draft(telegram_id), self.repository.get_active_round()
        if not draft or not prediction or not round_: return
        if self.now() >= round_.deadline_msk: self._deadline_screen(chat_id, telegram_id); return
        try: validate_prediction(prediction, round_, self.now())
        except ValidationError as error: self.telegram.send_message(chat_id, f"Прогноз не сохранён: {error}"); return
        created = self.repository.save_prediction(prediction, self._current_update_id)
        self._discard_draft(telegram_id)
        self._menu(chat_id, "Прогноз сохранён. До дедлайна его можно заменить полностью." if created else "Этот update уже был обработан: актуальный прогноз восстановлен.")

    def _show_my(self, chat_id: int, telegram_id: str) -> None:
        participant, round_ = self.repository.get_participant(telegram_id), self.repository.get_active_round()
        if not participant: self.telegram.send_message(chat_id, "Сначала выполните /start."); return
        if not round_: self.telegram.send_message(chat_id, "Вы зарегистрированы. Сейчас нет открытого тура."); return
        prediction = self.repository.get_prediction(round_.round_id, participant.participant_id)
        draft = self._draft(telegram_id)
        if draft and draft.round_id == round_.round_id and draft.chat_id == chat_id and self._deadline_guard(chat_id, telegram_id, draft):
            return
        if not prediction:
            if draft and draft.round_id == round_.round_id and draft.chat_id == chat_id:
                recovery = "\n⚠️ Карточка восстановления не была подтверждена. Нажмите «Продолжить черновик», чтобы создать новый экран." if draft.reanchor_pending else ""
                self._trace_draft(telegram_id, "my_draft_visible", draft, draft.phase, draft.active_message_id, draft.active_message_id, "success")
                self.telegram.send_message(
                    chat_id,
                    "📝 Есть незавершённый черновик.\n"
                    + self._draft_progress(draft) + recovery,
                    _keyboard([
                        ("▶️ Продолжить черновик", f"draft:resume:{draft.draft_id}:{draft.revision}"),
                        ("🔄 Начать заново", f"draft:restart:ask:{draft.draft_id}:{draft.revision}"),
                    ]),
                )
                return
            self.telegram.send_message(chat_id, "Прогноз ещё не подтверждён.", _keyboard([("📝 Собрать прогноз", "menu:new")])); return
        lines = ["🔒 Мой прогноз", f"\n🏟️ Тур: {round_.round_id}", f"⏰ Дедлайн: {round_.deadline_msk:%d.%m %H:%M}", "✅ Статус: подтверждён"]
        for bet in prediction.bets:
            odds = _combined_odds(bet)
            title = "ОРДИНАР" if bet.bet_type == BetType.SINGLE else "ЭКСПРЕСС"
            lines += [f"\n{title}"] + [f"⚽ {self._event_label(round_, item)}" for item in bet.events] + [f"💵 {bet.stake} × {odds} = {(Decimal(bet.stake) * odds).quantize(Decimal('1'), rounding=ROUND_HALF_UP)}"]
        lines.append("Итого банк: 5 000")
        if draft and draft.round_id == round_.round_id and draft.chat_id == chat_id:
            if draft.replacement_kind == "correction":
                lines.append("✏️ Есть черновик корректировок. Подтверждённый прогноз пока не изменён.")
                buttons = [("✏️ Продолжить корректировки", "resume"), ("✖️ Закрыть", "cancel")]
            else:
                lines.append("📝 Есть черновик полной замены. Подтверждённый прогноз пока не изменён.")
                buttons = [("📝 Продолжить замену", "resume"), ("✖️ Закрыть", "cancel")]
            if draft.phase != "M1":
                draft.return_phase = draft.phase
            draft.phase = "M1"
            self._touch(telegram_id)
            self._draft_screen(chat_id, telegram_id, "\n".join(lines), buttons)
            return
        buttons = []
        if self.now() < round_.deadline_msk:
            lines.append("До дедлайна можно внести корректировки или заменить прогноз полностью.")
            buttons = [("✏️ Внести корректировки", "correct"), ("🔄 Заменить прогноз", "replace"), ("✖️ Закрыть", "menu:close")]
        self.telegram.send_message(chat_id, "\n".join(lines), _keyboard(buttons) if buttons else None)

    def _back(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft: self._menu(chat_id, "Черновик не найден."); return
        if draft.phase == "E2": draft.phase = "E1"
        elif draft.phase == "E3": draft.phase = "E1"
        elif draft.phase == "X0": draft.phase = "E1"
        elif draft.phase == "X1": draft.phase = "X0" if len(draft.current_events) == 7 and draft.express_index == 0 else "E1"
        elif draft.phase == "X2": draft.phase = "E1"
        elif draft.phase == "B1":
            if draft.stake_edit is not None and draft.stake_edit_index:
                draft.stake_edit_index -= 1
                draft.stake_edit[draft.stake_edit_index] = None
            elif draft.stake_edit is not None:
                draft.stake_edit, draft.phase = None, "P1"
            else:
                draft.phase = "X2"
        elif draft.phase == "B2":
            self._edit_stake_again(chat_id, telegram_id); return
        elif draft.phase == "W1":
            self._cancel_pending_reset(chat_id, telegram_id); return
        elif draft.phase == "P1": draft.phase = "X2"
        elif draft.phase == "R1": draft.phase = "M1"
        elif draft.phase == "M1":
            self._menu(chat_id, "")
            return
        self._touch(telegram_id); self._render_current(chat_id, telegram_id)

    def _cancel_draft(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft: return
        if draft.phase != "C0":
            draft.return_phase = draft.phase
        draft.phase = "C0"; self._touch(telegram_id)
        kind = "корректировки" if draft.replacement_kind == "correction" else "замену" if draft.replacement_kind == "full" else "черновик"
        self._draft_screen(chat_id, telegram_id, f"Отменить {kind}? Подтверждённый прогноз, если есть, останется действующим.", [("✖️ Удалить черновик", "cancel:yes"), ("Продолжить", "resume")])

    def _deadline_screen(self, chat_id: int, telegram_id: str) -> None:
        draft = self._draft(telegram_id)
        if not draft: self._menu(chat_id, "Дедлайн прошёл."); return
        draft.phase = "L0"
        self._save_draft(telegram_id)
        text = "⏰ Дедлайн прошёл. Этот черновик нельзя подтвердить."
        if draft.active_message_id is None:
            # A recovered re-anchor intent deliberately has no active card.
            # Inform the user without creating a new draft/card contract.
            self.telegram.send_message(chat_id, text)
            return
        markup = self._draft_keyboard(draft, [("🔒 Мой прогноз", "menu:my"), ("✖️ Закрыть", "cancel:yes")])
        try:
            self._delivery_telegram.edit_message(chat_id, draft.active_message_id, text, markup)
        except TelegramHttpError as error:
            if error.kind != "message_not_modified":
                raise
        except UnknownDeliveryError:
            # The deadline state is already durable; no send fallback may
            # repeat a possibly applied Telegram side effect.
            logging.getLogger(__name__).warning("telegram_deadline_edit_unknown no_fallback=true")
            raise

    def _prediction_from_draft(self, telegram_id: str) -> Prediction | None:
        draft = self._draft(telegram_id)
        if not draft or len(draft.bets) != 5 or any(bet.stake is None for bet in draft.bets): return None
        participant = self.repository.get_participant(telegram_id)
        return Prediction(draft.round_id, participant.participant_id, tuple(Bet(bet.bet_type, bet.stake or 0, tuple(bet.events)) for bet in draft.bets), self.now()) if participant else None

    def _menu(self, chat_id: int, prefix: str) -> None:
        round_ = self.repository.get_active_round()
        if not round_: self.telegram.send_message(chat_id, f"{prefix}\nСейчас нет открытого тура."); return
        buttons = [("🔒 Мой прогноз", "menu:my"), ("❓ Помощь", "menu:help")]
        if self.now() < round_.deadline_msk: buttons.insert(0, ("📝 Собрать прогноз", "menu:new"))
        self.telegram.send_message(chat_id, f"⚽ Так или иначе\n🏟️ Тур: {round_.round_id}\n⏰ Дедлайн: {round_.deadline_msk:%d.%m %H:%M}\n{prefix}\nЧто хотите сделать?", _keyboard(buttons))

    @staticmethod
    def _help() -> str:
        return "❓ Помощь\n🎮 /start — открыть главное меню\n📝 /predict — собрать, продолжить или заменить прогноз\n🔒 /my — показать подтверждённый прогноз\n❓ /help — показать эту справку"


def _keyboard(buttons: list[tuple[str, str]], back: bool = False, cancel: bool = False) -> dict:
    rows = [[{"text": text, "callback_data": data}] for text, data in buttons]
    if back:
        rows.append([{"text": "Назад", "callback_data": "back"}])
    if cancel:
        rows.append([{"text": "Отмена", "callback_data": "cancel"}])
    return {"inline_keyboard": rows}


def _is_draft_action(data: str) -> bool:
    return data in {"back", "cancel", "express:add", "express:done", "confirm", "resume"} or data.startswith(("structure:", "match:", "market:", "stake:"))


def _combined_odds(bet: Bet) -> Decimal:
    result = Decimal("1")
    for event in bet.events:
        result *= event.odds_snapshot
    return result


def _chunks(lines: list[str], limit: int) -> list[list[str]]:
    chunks: list[list[str]] = []
    current: list[str] = []
    current_size = 0
    for line in lines:
        if current and current_size + len(line) + 1 > limit:
            chunks.append(current)
            current, current_size = [], 0
        current.append(line)
        current_size += len(line) + 1
    if current:
        chunks.append(current)
    return chunks
