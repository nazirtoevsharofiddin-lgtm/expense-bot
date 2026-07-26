#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот для трекинга расходов v6
С Claude AI парсером + Money Literacy система
"""

import os
import re
import csv
import json
from io import StringIO
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    filters, ContextTypes, ConversationHandler, CallbackQueryHandler
)
from telegram.constants import ParseMode

import gspread
from google.oauth2.service_account import Credentials
from anthropic import Anthropic


# ============================================================================
# КАТЕГОРИИ — 10 детальных + маппинг в 5 групп Money Literacy
# ============================================================================

EXPENSE_CATEGORIES = {
    "Еда и напитки": {
        "emoji": "🍽",
        "subcategories": ["Продукты", "Фастфуд", "Рестораны и кафе"],
        "literacy_group": "Быт",
    },
    "Покупки": {
        "emoji": "🛍",
        "subcategories": [
            "Одежда и обувь", "Украшения и аксессуары", "Здоровье и красота",
            "Дети", "Дом и сад", "Домашние животные", "Электроника",
            "Подарки", "Канцелярия и инструменты", "Аптека и бытовая химия",
        ],
        "literacy_group": "Быт",
    },
    "Жильё": {
        "emoji": "🏘",
        "subcategories": [
            "Аренда / Ипотека", "Коммунальные услуги",
            "Тех. обслуживание и ремонт", "Страхование имущества",
        ],
        "literacy_group": "Быт",
    },
    "Транспорт": {
        "emoji": "🚌",
        "subcategories": ["Общественный транспорт", "Такси", "Самолёт и авиабилеты"],
        "literacy_group": "Быт",
    },
    "Автомобиль": {
        "emoji": "🚗",
        "subcategories": [
            "Топливо", "Парковка", "Обслуживание автомобиля",
            "Прокат", "Страхование автомобиля",
        ],
        "literacy_group": "Быт",
    },
    "Жизнь и развлечения": {
        "emoji": "🎉",
        "subcategories": [
            "Медицина и врач", "Wellness и красота", "Спорт и фитнес",
            "Культура и события", "Хобби", "Подписки и стриминг",
            "Отпуск и отели", "Книги",
        ],
        "literacy_group": "Быт",
    },
    "Связь": {
        "emoji": "📱",
        "subcategories": ["Мобильный телефон", "Интернет", "ПО и приложения", "Почтовые услуги"],
        "literacy_group": "Быт",
    },
    "Образование": {
        "emoji": "📚",
        "subcategories": ["Курсы", "Книги (учёба)", "Подписки и каналы", "Другое"],
        "literacy_group": "Образование",
    },
    "Инвестиции": {
        "emoji": "📈",
        "subcategories": ["Недвижимость", "Движимое имущество", "Финансовые инвестиции", "Сбережения"],
        "literacy_group": "Инвестиции",
    },
    "Reciprocity": {
        "emoji": "❤️",
        "subcategories": [
            "Эхсон (помощь нуждающимся)",
            "Силаи рахм (подарки родственникам)",
            "Аҳли оила (деньги семье)",
        ],
        "literacy_group": "Reciprocity",
    },
    "Финансовые расходы": {
        "emoji": "💳",
        "subcategories": ["Налоги", "Страхование", "Займы и проценты", "Штрафы", "Комиссии"],
        "literacy_group": "Резерв",
    },
    "Долг": {
        "emoji": "🤝",
        "subcategories": ["Отдать долг", "Дать в долг кому-то", "Получить долг"],
        "literacy_group": "Резерв",
    },
    "Доход": {
        "emoji": "💵",
        "subcategories": ["Зарплата", "Проценты", "Продажи", "Доход от аренды", "Взносы и гранты", "Возвраты"],
        "literacy_group": "Доход",
    },
}

# 5 групп Money Literacy
LITERACY_GROUPS = {
    "Быт":         {"emoji": "🏠", "percent": 35, "color": "🟦"},
    "Образование":  {"emoji": "📚", "percent": 10, "color": "🟩"},
    "Инвестиции":  {"emoji": "💰", "percent": 10, "color": "🟨"},
    "Reciprocity": {"emoji": "❤️", "percent": 10, "color": "🟪"},
    "Резерв":      {"emoji": "🔓", "percent": 35, "color": "🟥"},
}

# Совместимость со старыми данными (раньше категория = группа literacy)
_OLD_CAT_TO_GROUP = {
    "Быт": "Быт",
    "Долги": "Резерв",
    "Образование": "Образование",
    "Инвестиции": "Инвестиции",
    "Reciprocity": "Reciprocity",
    "Доход": "Доход",
}

def get_literacy_group(category: str) -> str:
    if category in EXPENSE_CATEGORIES:
        return EXPENSE_CATEGORIES[category]["literacy_group"]
    # Старые данные — категория была именем literacy-группы
    return _OLD_CAT_TO_GROUP.get(category, "Быт")

WAITING_FOR_DELETE = range(1)


# ============================================================================
# ФОРМАТИРОВАНИЕ
# ============================================================================

def format_amount(amount: float) -> str:
    return f"{int(amount):,}".replace(",", " ")


def normalize_number(num_str: str) -> float:
    num_str = num_str.strip()
    if num_str.lower().endswith('k'):
        num_str = num_str[:-1]
        try:
            return float(num_str.replace(" ", "").replace("_", "")) * 1000
        except:
            pass
    num_str = num_str.replace(" ", "").replace("_", "")
    if "." in num_str or "," in num_str:
        if re.search(r'[.,]\d{2}$', num_str):
            num_str = num_str.replace(",", ".")
        else:
            num_str = num_str.replace(".", "").replace(",", "")
    return float(num_str)


def bar_chart(percent: float, width: int = 10) -> str:
    filled = min(int(percent / 100 * width), width)
    return "🟩" * filled + "⬜" * (width - filled)


# ============================================================================
# ТРЕКЕР РАСХОДОВ
# ============================================================================

class ExpenseTracker:
    def __init__(self, google_creds_path: str, sheet_id: str):
        creds_json_env = os.getenv("GOOGLE_CREDS_JSON")
        if creds_json_env:
            creds_info = json.loads(creds_json_env)
            print(f"🔑 Использую GOOGLE_CREDS_JSON, email={creds_info.get('client_email')}, key_id={creds_info.get('private_key_id')}")
            creds = Credentials.from_service_account_info(
                creds_info,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        else:
            with open(google_creds_path) as f:
                creds_info_debug = json.load(f)
            print(f"🔑 Использую файл {google_creds_path}, email={creds_info_debug.get('client_email')}, key_id={creds_info_debug.get('private_key_id')}")
            creds = Credentials.from_service_account_file(
                google_creds_path,
                scopes=["https://www.googleapis.com/auth/spreadsheets"]
            )
        self.gc = gspread.authorize(creds)
        self.sheet = self.gc.open_by_key(sheet_id)
        self.client = Anthropic()
        self._init_sheets()

    def _init_sheets(self):
        try:
            self.sheet.worksheet("Расходы")
        except:
            self.sheet.add_worksheet("Расходы", 2000, 8)
            ws = self.sheet.worksheet("Расходы")
            ws.append_row(["Дата", "Время", "Описание", "Подкатегория", "Категория", "Сумма", "Комментарий", "ID"])

    # ------------------------------------------------------------------
    # AI парсер
    # ------------------------------------------------------------------

    def parse_expense_ai(self, text: str) -> Optional[Dict]:
        cats_text = ""
        for cat, info in EXPENSE_CATEGORIES.items():
            subs = ", ".join(info["subcategories"])
            cats_text += f'- {cat}: {subs}\n'

        prompt = f"""Анализируй текст о расходе или доходе. Извлеки данные и верни ТОЛЬКО JSON.

Доступные категории и подкатегории:
{cats_text}

Текст: "{text}"

Правила:
- Еда, продукты, фрукты, напитки → "Еда и напитки"
- Саморазвитие, курсы, книги (учёба) → "Образование"
- Эхсон, садака, помощь нуждающимся → "Reciprocity" > "Эхсон (помощь нуждающимся)"
- Подарок/навещание родственников → "Reciprocity" > "Силаи рахм (подарки родственникам)"
- Деньги родителям, семье → "Reciprocity" > "Аҳли оила (деньги семье)"
- Золото, акции, вклад, сбережения → "Инвестиции"
- Зарплата, доход, премия, продажа → "Доход" (is_income: true)
- Отдать долг → "Долг" > "Отдать долг"
- Получить долг → "Долг" > "Получить долг" (is_income: true)
- amount всегда положительное число

Ответь ТОЛЬКО JSON без markdown:
{{"amount": 50000, "description": "кофе", "category": "Еда и напитки", "subcategory": "Рестораны и кафе", "is_income": false}}"""

        try:
            response = self.client.messages.create(
                model="claude-sonnet-5",
                max_tokens=250,
                messages=[{"role": "user", "content": prompt}]
            )
            text_response = response.content[0].text
            text_response = re.sub(r'```json\n?', '', text_response)
            text_response = re.sub(r'```\n?', '', text_response)
            text_response = text_response.strip()
            result = json.loads(text_response)
            return result
        except Exception as e:
            print(f"Ошибка Claude API: {e}")
            return None

    # ------------------------------------------------------------------
    # Запись расхода
    # ------------------------------------------------------------------

    def write_expense(self, amount: float, description: str, category: str,
                      subcategory: str, comment: str = "", is_income: bool = False) -> str:
        date = datetime.now().strftime("%d.%m.%Y")
        time_str = datetime.now().strftime("%H:%M")
        expense_id = f"{date}_{time_str}_{amount}".replace(".", "").replace(":", "")
        stored_amount = amount if is_income else -amount

        ws = self.sheet.worksheet("Расходы")
        ws.append_row([date, time_str, description, subcategory, category, stored_amount, comment, expense_id])

        cat_info = EXPENSE_CATEGORIES.get(category, {})
        emoji = cat_info.get("emoji", "💵")
        literacy_group = get_literacy_group(category)
        group_info = LITERACY_GROUPS.get(literacy_group, {})
        color = group_info.get("color", "⚪")

        if is_income:
            emoji = "💵"
            color = "💚"

        formatted_amt = format_amount(amount)
        sign = "+" if is_income else "-"

        msg = (
            f"{color} <b>{literacy_group}</b>  ›  {category}\n"
            f"{emoji} {sign}{formatted_amt} сум\n"
            f"📝 {description}\n"
            f"🏷 {subcategory}"
        )
        if comment:
            msg += f"\n💬 <i>{comment}</i>"
        return msg

    # ------------------------------------------------------------------
    # Получение расходов
    # ------------------------------------------------------------------

    def get_expenses(self, days: int = 30, limit: int = None) -> List[Tuple]:
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        cutoff_date = datetime.now() - timedelta(days=days)
        expenses = []
        for idx, row in enumerate(rows[1:], 1):
            if len(row) < 6:
                continue
            try:
                exp_date = datetime.strptime(row[0], "%d.%m.%Y")
                if exp_date >= cutoff_date:
                    expenses.append((
                        idx, row[0], row[1], row[2], row[3], row[4],
                        float(row[5]), row[6] if len(row) > 6 else ""
                    ))
            except:
                pass
        if limit:
            expenses = expenses[-limit:]
        return expenses[::-1]

    def delete_expense(self, row_idx: int) -> bool:
        ws = self.sheet.worksheet("Расходы")
        try:
            ws.delete_rows(row_idx + 1)
            return True
        except Exception as e:
            print(f"❌ delete_expense error (row_idx={row_idx}): {e}")
            return False

    # ------------------------------------------------------------------
    # СТАТИСТИКА 1: Money Literacy (5 групп)
    # ------------------------------------------------------------------

    def _get_rows_for_period(self, days: int):
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        cutoff = datetime.now() - timedelta(days=days)
        result = []
        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                if datetime.strptime(row[0], "%d.%m.%Y") >= cutoff:
                    result.append(row)
            except:
                pass
        return result

    def get_literacy_stats(self, days: int = 30) -> Tuple[str, InlineKeyboardMarkup]:
        rows = self._get_rows_for_period(days)

        group_totals: Dict[str, float] = {}
        total_income = 0.0
        total_expenses = 0.0

        for row in rows:
            try:
                category = row[4]
                amount = float(row[5])
                if amount > 0:
                    total_income += amount
                else:
                    group = get_literacy_group(category)
                    if group != "Доход":
                        group_totals[group] = group_totals.get(group, 0) + abs(amount)
                        total_expenses += abs(amount)
            except:
                pass

        period = f"за {days} дней" if days != 30 else "за месяц"
        msg = f"📊 <b>Money Literacy — {period}</b>\n\n"

        if total_income > 0:
            msg += f"💚 Доход: +{format_amount(total_income)} сум\n"
        if total_expenses > 0:
            msg += f"📉 Расходы: -{format_amount(total_expenses)} сум\n"
        balance = total_income - total_expenses
        sign = "+" if balance >= 0 else ""
        msg += f"💰 Баланс: {sign}{format_amount(balance)} сум\n\n"

        msg += "<b>Распределение по 5 группам:</b>\n\n"
        buttons = []
        for group, info in LITERACY_GROUPS.items():
            actual = group_totals.get(group, 0)
            plan_pct = info["percent"]
            actual_pct = (actual / total_expenses * 100) if total_expenses > 0 else 0
            status = "✅" if actual_pct <= plan_pct + 2 else "⚠️"
            bar = bar_chart(actual_pct)
            msg += (
                f"{info['emoji']} <b>{group}</b> {status}\n"
                f"  {bar} {actual_pct:.1f}% (план {plan_pct}%)\n"
                f"  {format_amount(actual)} сум\n\n"
            )
            if actual > 0:
                buttons.append([InlineKeyboardButton(
                    f"{info['emoji']} {group} — подробнее",
                    callback_data=f"literacy_detail|{group}|{days}"
                )])

        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        return msg, keyboard

    def get_literacy_group_detail(self, group: str, days: int = 30) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
        rows = self._get_rows_for_period(days)

        cat_totals: Dict[str, float] = {}
        subcat_totals: Dict[str, Dict[str, float]] = {}
        group_total = 0.0

        for row in rows:
            try:
                subcat = row[3]
                cat = row[4]
                amount = float(row[5])
                if amount >= 0:
                    continue
                if get_literacy_group(cat) != group:
                    continue
                abs_amt = abs(amount)
                cat_totals[cat] = cat_totals.get(cat, 0) + abs_amt
                subcat_totals.setdefault(cat, {})
                subcat_totals[cat][subcat] = subcat_totals[cat].get(subcat, 0) + abs_amt
                group_total += abs_amt
            except:
                pass

        info = LITERACY_GROUPS.get(group, {})
        emoji = info.get("emoji", "📁")
        period = f"за {days} дней" if days != 30 else "за месяц"
        msg = f"{emoji} <b>{group}</b> — {period}\n"
        msg += f"Итого: {format_amount(group_total)} сум\n\n"

        buttons = []
        for cat, cat_total in sorted(cat_totals.items(), key=lambda x: x[1], reverse=True):
            cat_info = EXPENSE_CATEGORIES.get(cat, {})
            cat_emoji = cat_info.get("emoji", "📁")
            cat_pct = (cat_total / group_total * 100) if group_total > 0 else 0
            msg += f"{cat_emoji} <b>{cat}</b> — {format_amount(cat_total)} сум ({cat_pct:.1f}%)\n"
            subs = subcat_totals.get(cat, {})
            for subcat, sub_total in sorted(subs.items(), key=lambda x: x[1], reverse=True):
                sub_pct = (sub_total / cat_total * 100) if cat_total > 0 else 0
                bar = bar_chart(sub_pct, width=8)
                msg += f"  • {subcat}\n    {bar} {sub_pct:.1f}% — {format_amount(sub_total)} сум\n"
            msg += "\n"
            buttons.append([InlineKeyboardButton(
                f"📋 {cat} — все записи",
                callback_data=f"cat_entries|{cat}|{days}"
            )])

        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        return msg, keyboard

    def get_category_entries(self, category: str, days: int = 30) -> str:
        """Список всех индивидуальных транзакций по категории."""
        rows = self._get_rows_for_period(days)
        entries = []
        cat_total = 0.0

        for row in rows:
            try:
                cat = row[4]
                if cat != category:
                    continue
                amount = float(row[5])
                if amount >= 0:
                    continue
                abs_amt = abs(amount)
                date_str = row[0]
                desc = row[2] or "—"
                subcat = row[3] or "—"
                entries.append((date_str, desc, subcat, abs_amt))
                cat_total += abs_amt
            except:
                pass

        cat_info = EXPENSE_CATEGORIES.get(category, {})
        emoji = cat_info.get("emoji", "📁")
        period = f"за {days} дней" if days != 30 else "за месяц"

        if not entries:
            return f"{emoji} <b>{category}</b> — {period}\n\nЗаписей нет."

        msg = f"{emoji} <b>{category}</b> — {period}\n"
        msg += f"Итого: {format_amount(cat_total)} сум | {len(entries)} записей\n\n"

        # Сортируем по дате (новые сначала)
        entries.sort(key=lambda x: x[0].split(".")[::-1], reverse=True)

        for date_str, desc, subcat, amt in entries:
            msg += f"📅 <b>{date_str}</b>  {desc}\n"
            msg += f"   ↳ {subcat} — {format_amount(amt)} сум\n"

        return msg

    # ------------------------------------------------------------------
    # СТАТИСТИКА 2: По 10 категориям + подкатегории
    # ------------------------------------------------------------------

    def get_category_stats(self, days: int = 30) -> Tuple[str, InlineKeyboardMarkup]:
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        cutoff = datetime.now() - timedelta(days=days)

        # Ремап старых имён в новые категории
        OLD_CAT_REMAP = {
            "Долги": "Долг",
            "Резерв": "Долг",
        }
        # Старые агрегированные категории, которые нельзя разбить
        ARCHIVE_CATS = {"Быт"}

        cat_totals: Dict[str, float] = {}
        subcat_totals: Dict[str, Dict[str, float]] = {}
        archive_totals: Dict[str, float] = {}
        total_expenses = 0.0

        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                exp_date = datetime.strptime(row[0], "%d.%m.%Y")
                if exp_date < cutoff:
                    continue
                subcat = row[3]
                cat = row[4]
                amount = float(row[5])
                if amount >= 0:
                    continue  # только расходы
                abs_amt = abs(amount)

                # Ремапим старые имена
                cat = OLD_CAT_REMAP.get(cat, cat)

                if cat in ARCHIVE_CATS:
                    archive_totals[cat] = archive_totals.get(cat, 0) + abs_amt
                else:
                    cat_totals[cat] = cat_totals.get(cat, 0) + abs_amt
                    if cat not in subcat_totals:
                        subcat_totals[cat] = {}
                    subcat_totals[cat][subcat] = subcat_totals[cat].get(subcat, 0) + abs_amt

                total_expenses += abs_amt
            except:
                pass

        period = f"за {days} дней" if days != 30 else "за месяц"
        msg = f"📂 <b>По категориям — {period}</b>\n\n"

        if not cat_totals and not archive_totals:
            return "📂 Нет данных за период", None

        sorted_cats = sorted(cat_totals.items(), key=lambda x: x[1], reverse=True)

        buttons = []
        for cat, total in sorted_cats:
            pct = (total / total_expenses * 100) if total_expenses > 0 else 0
            cat_info = EXPENSE_CATEGORIES.get(cat, {})
            emoji = cat_info.get("emoji", "📁")
            msg += f"{emoji} <b>{cat}</b>: {format_amount(total)} сум ({pct:.1f}%)\n"
            buttons.append([InlineKeyboardButton(
                f"📋 {cat} — подробнее",
                callback_data=f"subcat|{cat}|{days}"
            )])

        # Архивные данные (старые агрегированные)
        if archive_totals:
            msg += "\n<i>📦 Старые записи (нельзя разбить по подкатегориям):</i>\n"
            for cat, total in sorted(archive_totals.items(), key=lambda x: x[1], reverse=True):
                pct = (total / total_expenses * 100) if total_expenses > 0 else 0
                msg += f"📁 <i>{cat}</i>: {format_amount(total)} сум ({pct:.1f}%)\n"

        msg += f"\n<b>Итого расходов:</b> {format_amount(total_expenses)} сум"

        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        return msg, keyboard

    def get_subcategory_detail(self, category: str, days: int = 30) -> str:
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        cutoff = datetime.now() - timedelta(days=days)

        subcat_totals: Dict[str, float] = {}
        cat_total = 0.0

        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                exp_date = datetime.strptime(row[0], "%d.%m.%Y")
                if exp_date < cutoff:
                    continue
                if row[4] != category:
                    continue
                amount = float(row[5])
                if amount >= 0:
                    continue
                abs_amt = abs(amount)
                subcat = row[3] or "Без подкатегории"
                subcat_totals[subcat] = subcat_totals.get(subcat, 0) + abs_amt
                cat_total += abs_amt
            except:
                pass

        if not subcat_totals:
            return f"📂 Нет данных по категории «{category}»"

        cat_info = EXPENSE_CATEGORIES.get(category, {})
        emoji = cat_info.get("emoji", "📁")
        period = f"за {days} дней" if days != 30 else "за месяц"

        msg = f"{emoji} <b>{category}</b> — {period}\n"
        msg += f"Итого: {format_amount(cat_total)} сум\n\n"

        sorted_subs = sorted(subcat_totals.items(), key=lambda x: x[1], reverse=True)
        for subcat, total in sorted_subs:
            pct = (total / cat_total * 100) if cat_total > 0 else 0
            bar = bar_chart(pct, width=10)
            msg += f"  • <b>{subcat}</b>\n    {bar} {pct:.1f}% — {format_amount(total)} сум\n"

        return msg

    # ------------------------------------------------------------------
    # Прочие сводки
    # ------------------------------------------------------------------

    def get_top_expenses(self, limit: int = 5) -> str:
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        current_month = datetime.now().strftime("%m.%Y")
        expenses = []
        for row in rows[1:]:
            if len(row) < 6 or not row[0].endswith(current_month):
                continue
            try:
                amount = float(row[5])
                if amount < 0:
                    expenses.append((row[2], row[4], abs(amount)))
            except:
                pass
        expenses.sort(key=lambda x: x[2], reverse=True)
        if not expenses:
            return "📊 Нет расходов за этот месяц"
        msg = f"🏆 <b>Топ {limit} расходов месяца:</b>\n\n"
        for idx, (desc, cat, amount) in enumerate(expenses[:limit], 1):
            msg += f"{idx}. {format_amount(amount)} сум — {desc} ({cat})\n"
        total = sum(e[2] for e in expenses[:limit])
        msg += f"\n<b>Итого:</b> {format_amount(total)} сум"
        return msg

    def get_weekly_summary(self) -> str:
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        week_ago = datetime.now() - timedelta(days=7)
        income = 0.0
        expenses: Dict[str, float] = {}
        total_expenses = 0.0
        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                exp_date = datetime.strptime(row[0], "%d.%m.%Y")
                if exp_date >= week_ago:
                    cat = row[4]
                    amount = float(row[5])
                    if amount > 0:
                        income += amount
                    else:
                        expenses[cat] = expenses.get(cat, 0) + abs(amount)
                        total_expenses += abs(amount)
            except:
                pass
        if income == 0 and total_expenses == 0:
            return "📅 Нет данных за последние 7 дней"
        msg = "📅 <b>За неделю:</b>\n\n"
        if income > 0:
            msg += f"💚 <b>Доход:</b> +{format_amount(income)} сум\n"
        if total_expenses > 0:
            msg += f"📉 <b>Расходы:</b> -{format_amount(total_expenses)} сум\n"
        balance = income - total_expenses
        sign = "+" if balance >= 0 else ""
        msg += f"\n💰 <b>Баланс:</b> {sign}{format_amount(balance)} сум\n\n"
        if expenses:
            msg += "<b>По категориям:</b>\n"
            for cat, amt in sorted(expenses.items(), key=lambda x: x[1], reverse=True):
                cat_info = EXPENSE_CATEGORIES.get(cat, {})
                emoji = cat_info.get("emoji", "📁")
                msg += f"{emoji} {cat}: {format_amount(amt)} сум\n"
        return msg

    def get_month_summary(self) -> Tuple[str, Optional[InlineKeyboardMarkup]]:
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        current_month = datetime.now().strftime("%m.%Y")
        income = 0.0
        expenses_sum: Dict[str, float] = {}
        total_expenses = 0.0
        for row in rows[1:]:
            if len(row) < 6 or not row[0].endswith(current_month):
                continue
            try:
                cat = row[4]
                amount = float(row[5])
                if amount > 0:
                    income += amount
                else:
                    expenses_sum[cat] = expenses_sum.get(cat, 0) + abs(amount)
                    total_expenses += abs(amount)
            except:
                pass
        if income == 0 and total_expenses == 0:
            return "📈 Нет данных за этот месяц", None
        msg = "📈 <b>За месяц:</b>\n\n"
        if income > 0:
            msg += f"💚 <b>Доход:</b> +{format_amount(income)} сум\n"
        if total_expenses > 0:
            msg += f"📉 <b>Расходы:</b> -{format_amount(total_expenses)} сум\n"
        balance = income - total_expenses
        sign = "+" if balance >= 0 else ""
        msg += f"💰 <b>Баланс:</b> {sign}{format_amount(balance)} сум\n\n"
        msg += "<b>По категориям:</b>\n"

        # Показываем все категории из EXPENSE_CATEGORIES (включая нулевые)
        buttons = []
        # Сначала те у которых есть данные (по убыванию), потом нулевые
        cats_with_data = sorted(
            [(cat, expenses_sum.get(cat, 0)) for cat in EXPENSE_CATEGORIES if cat != "Доход"],
            key=lambda x: x[1], reverse=True
        )
        for cat, amt in cats_with_data:
            pct = (amt / total_expenses * 100) if total_expenses > 0 and amt > 0 else 0
            cat_info = EXPENSE_CATEGORIES.get(cat, {})
            emoji = cat_info.get("emoji", "📁")
            if amt > 0:
                msg += f"{emoji} {cat}: {format_amount(amt)} сум ({pct:.1f}%)\n"
                buttons.append([InlineKeyboardButton(
                    f"📋 {cat} — подробнее",
                    callback_data=f"cat_entries|{cat}|30"
                )])
            else:
                msg += f"{emoji} {cat}: 0 сум\n"

        # Старые агрегированные категории (не в EXPENSE_CATEGORIES)
        old_cats = {k: v for k, v in expenses_sum.items() if k not in EXPENSE_CATEGORIES and k != "Доход"}
        if old_cats:
            msg += "\n<i>📦 Старые записи:</i>\n"
            for cat, amt in sorted(old_cats.items(), key=lambda x: x[1], reverse=True):
                pct = (amt / total_expenses * 100) if total_expenses > 0 else 0
                msg += f"📁 <i>{cat}</i>: {format_amount(amt)} сум ({pct:.1f}%)\n"

        keyboard = InlineKeyboardMarkup(buttons) if buttons else None
        return msg, keyboard

    def get_year_summary(self) -> str:
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        year_ago = datetime.now() - timedelta(days=365)
        income = 0.0
        expenses_sum: Dict[str, float] = {}
        total_expenses = 0.0
        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                exp_date = datetime.strptime(row[0], "%d.%m.%Y")
                if exp_date >= year_ago:
                    cat = row[4]
                    amount = float(row[5])
                    if amount > 0:
                        income += amount
                    else:
                        expenses_sum[cat] = expenses_sum.get(cat, 0) + abs(amount)
                        total_expenses += abs(amount)
            except:
                pass
        if income == 0 and total_expenses == 0:
            return "📊 Нет данных за год"
        msg = "📊 <b>За год:</b>\n\n"
        if income > 0:
            msg += f"💚 <b>Доход:</b> +{format_amount(income)} сум\n"
        if total_expenses > 0:
            msg += f"📉 <b>Расходы:</b> -{format_amount(total_expenses)} сум\n"
        balance = income - total_expenses
        sign = "+" if balance >= 0 else ""
        msg += f"\n💰 <b>Баланс:</b> {sign}{format_amount(balance)} сум\n\n"
        msg += "<b>Расходы по категориям:</b>\n"
        for cat, amt in sorted(expenses_sum.items(), key=lambda x: x[1], reverse=True):
            pct = (amt / total_expenses * 100) if total_expenses > 0 else 0
            cat_info = EXPENSE_CATEGORIES.get(cat, {})
            emoji = cat_info.get("emoji", "📁")
            msg += f"{emoji} {cat}: {format_amount(amt)} сум ({pct:.1f}%)\n"
        return msg

    def get_today_summary(self) -> str:
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        today = datetime.now().strftime("%d.%m.%Y")
        income = 0.0
        expenses_sum: Dict[str, float] = {}
        total_expenses = 0.0
        for row in rows[1:]:
            if len(row) < 6 or row[0] != today:
                continue
            try:
                cat = row[4]
                amount = float(row[5])
                if amount > 0:
                    income += amount
                else:
                    expenses_sum[cat] = expenses_sum.get(cat, 0) + abs(amount)
                    total_expenses += abs(amount)
            except:
                pass
        if income == 0 and total_expenses == 0:
            return "📊 Нет данных сегодня"
        msg = "📊 <b>Сегодня:</b>\n\n"
        if income > 0:
            msg += f"💚 <b>Доход:</b> +{format_amount(income)} сум\n"
        if total_expenses > 0:
            msg += f"📉 <b>Расходы:</b> -{format_amount(total_expenses)} сум\n"
            for cat, amt in sorted(expenses_sum.items(), key=lambda x: x[1], reverse=True):
                cat_info = EXPENSE_CATEGORIES.get(cat, {})
                emoji = cat_info.get("emoji", "📁")
                msg += f"  {emoji} {cat}: {format_amount(amt)} сум\n"
        balance = income - total_expenses
        sign = "+" if balance >= 0 else ""
        msg += f"\n💰 <b>Баланс:</b> {sign}{format_amount(balance)} сум"
        return msg

    def get_quick_stats(self) -> str:
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        today = datetime.now().strftime("%d.%m.%Y")
        week_ago = datetime.now() - timedelta(days=7)
        month_ago = datetime.now() - timedelta(days=30)
        ti, te, wi, we, mi, me = 0.0, 0.0, 0.0, 0.0, 0.0, 0.0
        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                date_str = row[0]
                exp_date = datetime.strptime(date_str, "%d.%m.%Y")
                amount = float(row[5])
                if date_str == today:
                    if amount > 0: ti += amount
                    else: te += abs(amount)
                if exp_date >= week_ago:
                    if amount > 0: wi += amount
                    else: we += abs(amount)
                if exp_date >= month_ago:
                    if amount > 0: mi += amount
                    else: me += abs(amount)
            except:
                pass
        return (
            f"⚡ <b>Быстрая статистика</b>\n\n"
            f"📅 <b>Сегодня:</b> +{format_amount(ti)} / -{format_amount(te)} = {format_amount(ti-te)}\n"
            f"📆 <b>Неделя:</b>  +{format_amount(wi)} / -{format_amount(we)} = {format_amount(wi-we)}\n"
            f"📊 <b>Месяц:</b>   +{format_amount(mi)} / -{format_amount(me)} = {format_amount(mi-me)}"
        )

    def get_trends(self) -> str:
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        this_month = datetime.now().strftime("%m.%Y")
        prev_month = (datetime.now() - timedelta(days=30)).strftime("%m.%Y")
        this_total = 0.0
        prev_total = 0.0
        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                date_str = row[0]
                amount = float(row[5])
                if amount < 0:
                    if date_str.endswith(this_month):
                        this_total += abs(amount)
                    elif date_str.endswith(prev_month):
                        prev_total += abs(amount)
            except:
                pass
        if prev_total == 0:
            return "📈 Недостаточно данных для анализа тренда"
        pct = ((this_total - prev_total) / prev_total) * 100
        if pct > 10:
            trend = "📈 <b>Расходы растут!</b>"
        elif pct < -10:
            trend = "📉 <b>Расходы падают!</b>"
        else:
            trend = "➡️ <b>Расходы стабильны</b>"
        sign = "+" if pct > 0 else ""
        return (
            f"{trend}\n\n"
            f"Прошлый месяц: {format_amount(prev_total)} сум\n"
            f"Этот месяц: {format_amount(this_total)} сум\n"
            f"Изменение: {sign}{pct:.1f}%"
        )

    def compare_months(self, month1_str: str, month2_str: str) -> str:
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        income1, income2 = 0.0, 0.0
        exp1: Dict[str, float] = {}
        exp2: Dict[str, float] = {}
        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                date_str = row[0]
                cat = row[4]
                amount = float(row[5])
                if date_str.endswith(month1_str):
                    if amount > 0: income1 += amount
                    else: exp1[cat] = exp1.get(cat, 0) + abs(amount)
                elif date_str.endswith(month2_str):
                    if amount > 0: income2 += amount
                    else: exp2[cat] = exp2.get(cat, 0) + abs(amount)
            except:
                pass
        total1 = income1 - sum(exp1.values())
        total2 = income2 - sum(exp2.values())
        msg = f"<b>Сравнение {month1_str} vs {month2_str}</b>\n\n"
        msg += f"💚 Доход: {format_amount(income1)} → {format_amount(income2)} сум\n"
        msg += f"📉 Расходы: {format_amount(sum(exp1.values()))} → {format_amount(sum(exp2.values()))} сум\n"
        msg += f"💰 Баланс: {format_amount(total1)} → {format_amount(total2)} сум"
        return msg

    def export_csv(self) -> str:
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(rows)
        return output.getvalue()


# ============================================================================
# ОБРАБОТЧИКИ КОМАНД
# ============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 <b>Привет! Я трекер расходов с ИИ</b>\n\n"
        "📝 <b>Просто пиши что потратил:</b>\n"
        "• купил арбуз 50 000\n"
        "• курс по программированию 500к\n"
        "• зарплата 8 500 000\n"
        "• эхсон 100 000\n\n"
        "ИИ сам определит категорию!\n\n"
        "⚙️ <b>Команды:</b>\n"
        "/today — сегодня\n"
        "/quick — быстро\n"
        "/weekly — неделя\n"
        "/month — месяц\n"
        "/year — год\n"
        "/literacy — Money Literacy (5 групп)\n"
        "/top — топ 5 расходов\n"
        "/trends — тренды\n"
        "/compare 01.2026 12.2025 — сравнить\n"
        "/undo — удалить запись\n"
        "/export — скачать CSV\n"
        "/help — справка"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    if not tracker:
        await update.message.reply_text("❌ Ошибка инициализации")
        return
    text = update.message.text.strip()
    result = tracker.parse_expense_ai(text)
    if not result or result.get('amount') is None:
        await update.message.reply_text("❓ Не смог распарсить. Попробуй ещё раз.")
        return
    msg = tracker.write_expense(
        float(result['amount']), result.get('description', text),
        result.get('category', 'Покупки'), result.get('subcategory', ''),
        "", result.get('is_income', False)
    )
    await update.message.reply_text(f"✅\n\n{msg}", parse_mode=ParseMode.HTML)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    await update.message.reply_text(tracker.get_today_summary(), parse_mode=ParseMode.HTML)


async def quick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    await update.message.reply_text(tracker.get_quick_stats(), parse_mode=ParseMode.HTML)


async def weekly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    await update.message.reply_text(tracker.get_weekly_summary(), parse_mode=ParseMode.HTML)


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    msg, keyboard = tracker.get_month_summary()
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def year_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    await update.message.reply_text(tracker.get_year_summary(), parse_mode=ParseMode.HTML)


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    await update.message.reply_text(tracker.get_top_expenses(5), parse_mode=ParseMode.HTML)


async def trends_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    await update.message.reply_text(tracker.get_trends(), parse_mode=ParseMode.HTML)


async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    args = update.message.text.split()
    if len(args) < 3:
        await update.message.reply_text("Формат: /compare 01.2026 12.2025")
        return
    await update.message.reply_text(tracker.compare_months(args[1], args[2]), parse_mode=ParseMode.HTML)


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    csv_data = tracker.export_csv()
    await update.message.reply_document(document=csv_data.encode(), filename="расходы.csv")


# --- Статистика 1: Money Literacy ---
async def literacy_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    args = update.message.text.split()
    days = 30
    if len(args) > 1:
        try:
            days = int(args[1])
        except:
            pass
    msg, keyboard = tracker.get_literacy_stats(days)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def literacy_detail_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tracker = context.bot_data.get('tracker')
    parts = query.data.split("|")
    if len(parts) < 3:
        return
    _, group, days_str = parts
    msg, keyboard = tracker.get_literacy_group_detail(group, int(days_str))
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def cat_entries_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Показывает все записи по конкретной категории."""
    query = update.callback_query
    await query.answer()
    tracker = context.bot_data.get('tracker')
    parts = query.data.split("|")
    if len(parts) < 3:
        return
    _, category, days_str = parts
    msg = tracker.get_category_entries(category, int(days_str))
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def subcat_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    tracker = context.bot_data.get('tracker')
    parts = query.data.split("|")
    if len(parts) < 3:
        return
    _, category, days_str = parts
    days = int(days_str)
    msg = tracker.get_subcategory_detail(category, days)
    await query.message.reply_text(msg, parse_mode=ParseMode.HTML)


# --- Удаление ---
async def undo_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    args = update.message.text.split()
    limit = 5
    if len(args) > 1:
        if args[1] == "all":
            limit = 100
        else:
            try:
                limit = int(args[1])
            except:
                limit = 5
    expenses = tracker.get_expenses(days=30, limit=limit)
    if not expenses:
        await update.message.reply_text("❌ Нет расходов")
        return
    msg = "📋 <b>Какой удалить?</b>\n\n"
    for idx, (row_idx, date, time_str, desc, subcat, cat, amount, comment) in enumerate(expenses, 1):
        formatted_amt = format_amount(abs(amount))
        sign = "+" if amount > 0 else "-"
        msg += f"{idx}️⃣ {sign}{formatted_amt} сум | {desc} | {date}\n"
    msg += "\n(Ответь числом)"
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)
    context.user_data['expenses_to_delete'] = expenses
    return WAITING_FOR_DELETE


async def handle_delete_choice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    expenses = context.user_data.get('expenses_to_delete', [])
    if not expenses:
        await update.message.reply_text("❌ Сессия истекла")
        return ConversationHandler.END
    try:
        choice = int(update.message.text.strip())
        if 1 <= choice <= len(expenses):
            row_idx, date, time_str, desc, subcat, cat, amount, comment = expenses[choice - 1]
            if tracker.delete_expense(row_idx):
                formatted_amt = format_amount(abs(amount))
                sign = "+" if amount > 0 else "-"
                await update.message.reply_text(
                    f"✅ Удалено!\n\n{sign}{formatted_amt} сум | {desc}",
                    parse_mode=ParseMode.HTML
                )
            else:
                await update.message.reply_text("❌ Ошибка при удалении")
        else:
            await update.message.reply_text(f"❌ Введи число от 1 до {len(expenses)}")
    except ValueError:
        await update.message.reply_text("❌ Напиши число")
    return ConversationHandler.END


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ <b>Справка</b>\n\n"
        "<b>Money Literacy — 5 групп:</b>\n"
        "🏠 Быт 35%  |  📚 Образование 10%\n"
        "💰 Инвестиции 10%  |  ❤️ Reciprocity 10%\n"
        "🔓 Резерв 35% (свободные деньги)\n\n"
        "<b>Reciprocity (10%) — 3 части:</b>\n"
        "• Эхсон — помощь нуждающимся\n"
        "• Силаи рахм — подарки родственникам\n"
        "• Аҳли оила — деньги своим\n\n"
        "<b>Статистика:</b>\n"
        "/literacy — Money Literacy (5 групп + детали по каждой)\n\n"
        "<b>Форматы суммы:</b>\n"
        "5000, 5 000, 5.000, 25k — все работают"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


# ============================================================================
# ГЛАВНАЯ ФУНКЦИЯ
# ============================================================================

def main():
    TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
    GOOGLE_CREDS = os.getenv("GOOGLE_CREDS_PATH", "credentials.json")
    SHEET_ID = os.getenv("GOOGLE_SHEET_ID")

    if not TOKEN or not SHEET_ID:
        print("❌ Установи TELEGRAM_BOT_TOKEN и GOOGLE_SHEET_ID")
        return
    if not os.getenv("GOOGLE_CREDS_JSON") and not os.path.exists(GOOGLE_CREDS):
        print("❌ Нужна переменная GOOGLE_CREDS_JSON или файл credentials.json")
        return

    print("🚀 Инициализация с Claude AI...")
    tracker = ExpenseTracker(GOOGLE_CREDS, SHEET_ID)
    print("✅ Готов")

    print("🤖 Запуск бота...")
    app = Application.builder().token(TOKEN).build()
    app.bot_data['tracker'] = tracker

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("today", today_cmd))
    app.add_handler(CommandHandler("quick", quick_cmd))
    app.add_handler(CommandHandler("weekly", weekly_cmd))
    app.add_handler(CommandHandler("month", month_cmd))
    app.add_handler(CommandHandler("year", year_cmd))
    app.add_handler(CommandHandler("top", top_cmd))
    app.add_handler(CommandHandler("trends", trends_cmd))
    app.add_handler(CommandHandler("compare", compare_cmd))
    app.add_handler(CommandHandler("export", export_cmd))
    app.add_handler(CommandHandler("literacy", literacy_cmd))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("undoall", undoall_cmd))
    app.add_handler(CallbackQueryHandler(subcat_callback, pattern=r"^subcat\|"))
    app.add_handler(CallbackQueryHandler(literacy_detail_callback, pattern=r"^literacy_detail\|"))
    app.add_handler(CallbackQueryHandler(cat_entries_callback, pattern=r"^cat_entries\|"))

    app.add_handler(CallbackQueryHandler(delete_expense_callback, pattern=r"^delete_exp|"))
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("undo", undo_cmd)],
        states={
            WAITING_FOR_DELETE: [MessageHandler(filters.TEXT & ~filters.COMMAND, handle_delete_choice)]
        },
        fallbacks=[CommandHandler("start", start)]
    )
    app.add_handler(conv_handler)
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    print("✅ Бот запущен с Claude AI")
    app.run_polling()


if __name__ == "__main__":
    main()
