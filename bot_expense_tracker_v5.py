#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Telegram бот для трекинга расходов v5
С Claude AI парсером
"""

import os
import re
import csv
from io import StringIO
from datetime import datetime, timedelta
from typing import Optional, Dict, List, Tuple

from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, filters, ContextTypes, ConversationHandler
from telegram.constants import ParseMode

import gspread
from google.oauth2.service_account import Credentials
from anthropic import Anthropic


# ============================================================================
# КАТЕГОРИИ И РАСПРЕДЕЛЕНИЕ
# ============================================================================

SYSTEM_CATEGORIES = {
    "Быт": {"emoji": "🏠", "percent": 35, "color": "🟦"},
    "Образование": {"emoji": "📚", "percent": 10, "color": "🟩"},
    "Инвестиции": {"emoji": "💰", "percent": 10, "color": "🟨"},
    "Reciprocity": {"emoji": "❤️", "percent": 10, "color": "🟪"},
    "Долги": {"emoji": "💳", "percent": 35, "color": "🟥"},
}

WAITING_FOR_DELETE = range(1)


# ============================================================================
# ФОРМАТИРОВАНИЕ
# ============================================================================

def format_amount(amount: float) -> str:
    """Форматирует число с пробелами: 50000 -> 50 000"""
    return f"{int(amount):,}".replace(",", " ")


def normalize_number(num_str: str) -> float:
    """Нормализует число: 5000, 5 000, 5.000, 5,000, 25k, 25K"""
    num_str = num_str.strip()
    
    # Поддержка k и K суффикса (25k = 25000)
    if num_str.lower().endswith('k'):
        num_str = num_str[:-1]
        try:
            return float(num_str.replace(" ", "").replace("_", "")) * 1000
        except:
            pass
    
    # Убираем пробелы и подчеркивания
    num_str = num_str.replace(" ", "").replace("_", "")
    
    # Если есть точка или запятая
    if "." in num_str or "," in num_str:
        if re.search(r'[.,]\d{2}$', num_str):
            num_str = num_str.replace(",", ".")
        else:
            num_str = num_str.replace(".", "").replace(",", "")
    
    return float(num_str)


# ============================================================================
# ТРЕКЕР РАСХОДОВ
# ============================================================================

class ExpenseTracker:
    def __init__(self, google_creds_path: str, sheet_id: str):
        """Инициализация с Google Sheets и Claude AI"""
        creds = Credentials.from_service_account_file(
            google_creds_path,
            scopes=["https://www.googleapis.com/auth/spreadsheets"]
        )
        self.gc = gspread.authorize(creds)
        self.sheet = self.gc.open_by_key(sheet_id)
        self.client = Anthropic()
        self._init_sheets()

    def _init_sheets(self):
        """Создаёт листы если их нет"""
        try:
            self.sheet.worksheet("Расходы")
        except:
            self.sheet.add_worksheet("Расходы", 2000, 8)
            ws = self.sheet.worksheet("Расходы")
            ws.append_row(["Дата", "Время", "Описание", "Подкатегория", "Категория", "Сумма", "Комментарий", "ID"])

    def parse_expense_ai(self, text: str) -> Optional[Dict]:
        """Парсит расход через Claude AI"""
        prompt = f"""Анализируй текст о расходе. Извлеки:
1. Сумму (только цифра)
2. Описание (что это)
3. Категорию: Быт, Образование, Инвестиции, Reciprocity, Долги, Доход
4. Подкатегорию

Подкатегории по категориям:
- Быт: Еда и напитки, Покупки, Жилье, Транспорт, Связь, Жизнь и развлечения
- Образование: Книги, Образование и развитие, Подписки, Аудио
- Инвестиции: Недвижимость, Финансовые инвестиции, Сбережения, Движимое имущество
- Reciprocity: Благотворительность, Подарки, Помощь семье
- Долги: Отдать долг, Получить долг, Налоги, Займы, Штрафы, Комиссии
- Доход: Зарплата, Доход, Премия

Текст: "{text}"

Ответь ТОЛЬКО JSON:
{{"amount": 50000, "description": "кофе", "category": "Быт", "subcategory": "Еда и напитки", "is_income": false}}

ВАЖНО:
- Если это еда/напитки/фрукты/овощи/продукты → Быт > Еда и напитки
- Если это парк/кино/театр/развлечение → Быт > Жизнь и развлечения
- Если это зарплата/доход/премия → категория Доход
- Если это деньги на помощь семье/благотворительность → Reciprocity
- amount всегда положительное число
- is_income: true только для доходов (зарплата, премия, доход)"""
        
        try:
            response = self.client.messages.create(
                model="claude-opus-4-6",
                max_tokens=200,
                messages=[{"role": "user", "content": prompt}]
            )
            text_response = response.content[0].text
            # Очистить от markdown
            text_response = re.sub(r'```json\n?', '', text_response)
            text_response = re.sub(r'```\n?', '', text_response)
            text_response = text_response.strip()
            
            result = eval(text_response)  # Парсим JSON
            return result
        except Exception as e:
            print(f"Ошибка Claude API: {e}")
            return None

    def write_expense(self, amount: float, description: str, category: str, subcategory: str, comment: str = "", is_income: bool = False) -> str:
        """Пишет расход или доход в Sheets"""
        date = datetime.now().strftime("%d.%m.%Y")
        time = datetime.now().strftime("%H:%M")
        expense_id = f"{date}_{time}_{amount}".replace(".", "").replace(":", "")
        
        # Для дохода положительная, для расходов отрицательная
        stored_amount = amount if is_income else -amount
        
        ws = self.sheet.worksheet("Расходы")
        ws.append_row([date, time, description, subcategory, category, stored_amount, comment, expense_id])
        
        if is_income:
            color = "💚"
            emoji = "💵"
        else:
            cat_info = SYSTEM_CATEGORIES.get(category, {})
            emoji = cat_info.get("emoji", "💵")
            color = cat_info.get("color", "⚪")
        
        formatted_amt = format_amount(amount)
        sign = "+" if is_income else "-"
        
        msg = (
            f"{color} <b>{category}</b>\n"
            f"{emoji} {sign}{formatted_amt} сум\n"
            f"📝 {description}\n"
            f"🏷 {subcategory}"
        )
        if comment:
            msg += f"\n💬 <i>{comment}</i>"
        
        return msg

    def get_expenses(self, days: int = 30, limit: int = None) -> List[Tuple]:
        """Получает расходы за период"""
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
                    expenses.append((idx, row[0], row[1], row[2], row[3], row[4], float(row[5]), row[6] if len(row) > 6 else ""))
            except:
                pass
        
        if limit:
            expenses = expenses[-limit:]
        
        return expenses[::-1]

    def delete_expense(self, row_idx: int) -> bool:
        """Удаляет расход по индексу строки"""
        ws = self.sheet.worksheet("Расходы")
        try:
            ws.delete_rows(row_idx, 1)
            return True
        except:
            return False

    def get_top_expenses(self, limit: int = 5) -> str:
        """Топ расходов за месяц"""
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        
        current_month = datetime.now().strftime("%m.%Y")
        expenses = []
        
        for row in rows[1:]:
            if len(row) < 6 or not row[0].endswith(current_month):
                continue
            try:
                amount = float(row[5])
                if amount < 0:  # Только расходы
                    expenses.append((row[2], row[4], abs(amount)))
            except:
                pass
        
        expenses.sort(key=lambda x: x[2], reverse=True)
        
        if not expenses:
            return "📊 Нет расходов за этот месяц"
        
        msg = f"🏆 <b>Топ {limit} расходов этого месяца:</b>\n\n"
        for idx, (desc, cat, amount) in enumerate(expenses[:limit], 1):
            formatted_amt = format_amount(amount)
            msg += f"{idx}. {formatted_amt} сум — {desc} ({cat})\n"
        
        total = sum(e[2] for e in expenses[:limit])
        msg += f"\n<b>Итого:</b> {format_amount(total)} сум"
        return msg

    def get_weekly_summary(self) -> str:
        """Сводка за неделю"""
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        
        week_ago = datetime.now() - timedelta(days=7)
        income = 0
        expenses = {}
        total_expenses = 0
        
        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                exp_date = datetime.strptime(row[0], "%d.%m.%Y")
                if exp_date >= week_ago:
                    category = row[4]
                    amount = float(row[5])
                    
                    if amount > 0:
                        income += amount
                    else:
                        expenses[category] = expenses.get(category, 0) + abs(amount)
                        total_expenses += abs(amount)
            except:
                pass
        
        if income == 0 and total_expenses == 0:
            return "📅 Нет данных за последние 7 дней"
        
        msg = f"📅 <b>За неделю:</b>\n\n"
        if income > 0:
            msg += f"💚 <b>Доход:</b> +{format_amount(income)} сум\n"
        if total_expenses > 0:
            msg += f"📉 <b>Расходы:</b> -{format_amount(total_expenses)} сум\n"
        
        balance = income - total_expenses
        sign = "+" if balance >= 0 else ""
        msg += f"\n💰 <b>Баланс:</b> {sign}{format_amount(balance)} сум\n\n"
        
        if expenses:
            msg += "<b>По категориям:</b>\n"
            for category in ["Быт", "Образование", "Инвестиции", "Reciprocity", "Долги"]:
                if category in expenses:
                    amount = expenses[category]
                    cat_info = SYSTEM_CATEGORIES.get(category, {})
                    emoji = cat_info.get("emoji", "💵")
                    msg += f"{emoji} {category}: {format_amount(amount)} сум\n"
        
        return msg

    def get_year_summary(self) -> str:
        """Статистика за год"""
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        
        year_ago = datetime.now() - timedelta(days=365)
        income = 0
        expenses_sum = {}
        total_expenses = 0
        
        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                exp_date = datetime.strptime(row[0], "%d.%m.%Y")
                if exp_date >= year_ago:
                    category = row[4]
                    amount = float(row[5])
                    
                    if amount > 0:
                        income += amount
                    else:
                        expenses_sum[category] = expenses_sum.get(category, 0) + abs(amount)
                        total_expenses += abs(amount)
            except:
                pass
        
        if income == 0 and total_expenses == 0:
            return "📊 Нет данных за год"
        
        msg = f"📊 <b>За год:</b>\n\n"
        if income > 0:
            msg += f"💚 <b>Доход:</b> +{format_amount(income)} сум\n"
        if total_expenses > 0:
            msg += f"📉 <b>Расходы:</b> -{format_amount(total_expenses)} сум\n"
        
        balance = income - total_expenses
        sign = "+" if balance >= 0 else ""
        msg += f"\n💰 <b>Баланс:</b> {sign}{format_amount(balance)} сум\n\n"
        
        if expenses_sum:
            msg += "<b>Расходы по категориям:</b>\n"
            for category in ["Быт", "Образование", "Инвестиции", "Reciprocity", "Долги"]:
                if category in expenses_sum:
                    amount = expenses_sum[category]
                    cat_info = SYSTEM_CATEGORIES.get(category, {})
                    emoji = cat_info.get("emoji", "💵")
                    budget_percent = cat_info.get("percent", 0)
                    
                    bar = "🟩" * int((amount / total_expenses * 100) / 5) if total_expenses > 0 else ""
                    bar += "⬜" * (20 - len(bar))
                    
                    msg += (
                        f"{emoji} {category}: {format_amount(amount)} сум\n"
                        f"   {bar}\n"
                        f"   Бюджет: {budget_percent}%\n\n"
                    )
        
        return msg

    def compare_months(self, month1_str: str, month2_str: str) -> str:
        """Сравнивает два месяца"""
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        
        income1 = 0
        expenses1 = {}
        income2 = 0
        expenses2 = {}
        
        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                date_str = row[0]
                category = row[4]
                amount = float(row[5])
                
                if date_str.endswith(month1_str):
                    if amount > 0:
                        income1 += amount
                    else:
                        expenses1[category] = expenses1.get(category, 0) + abs(amount)
                elif date_str.endswith(month2_str):
                    if amount > 0:
                        income2 += amount
                    else:
                        expenses2[category] = expenses2.get(category, 0) + abs(amount)
            except:
                pass
        
        total1 = income1 - sum(expenses1.values())
        total2 = income2 - sum(expenses2.values())
        
        msg = f"<b>Сравнение {month1_str} vs {month2_str}</b>\n\n"
        msg += f"💚 Доход: {format_amount(income1)} → {format_amount(income2)}\n"
        msg += f"📉 Расходы: {format_amount(sum(expenses1.values()))} → {format_amount(sum(expenses2.values()))}\n"
        msg += f"💰 Баланс: {format_amount(total1)} → {format_amount(total2)}\n"
        
        return msg

    def get_trends(self) -> str:
        """Анализирует тренды расходов"""
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        
        this_month = datetime.now().strftime("%m.%Y")
        prev_month = (datetime.now() - timedelta(days=30)).strftime("%m.%Y")
        
        this_total = 0
        prev_total = 0
        
        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                date_str = row[0]
                amount = float(row[5])
                
                if amount < 0:  # Только расходы
                    if date_str.endswith(this_month):
                        this_total += abs(amount)
                    elif date_str.endswith(prev_month):
                        prev_total += abs(amount)
            except:
                pass
        
        if prev_total == 0:
            return "📈 Недостаточно данных для анализа тренда"
        
        percent_change = ((this_total - prev_total) / prev_total) * 100
        
        if percent_change > 10:
            trend = "📈 <b>Расходы растут!</b>"
        elif percent_change < -10:
            trend = "📉 <b>Расходы падают!</b>"
        else:
            trend = "➡️ <b>Расходы стабильны</b>"
        
        msg = f"{trend}\n\n"
        msg += f"Прошлый месяц: {format_amount(prev_total)} сум\n"
        msg += f"Этот месяц: {format_amount(this_total)} сум\n"
        sign = "+" if percent_change > 0 else ""
        msg += f"Изменение: {sign}{percent_change:.1f}%"
        
        return msg

    def get_quick_stats(self) -> str:
        """Быстрая статистика"""
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        
        today = datetime.now().strftime("%d.%m.%Y")
        week_ago = datetime.now() - timedelta(days=7)
        month_ago = datetime.now() - timedelta(days=30)
        
        today_income = 0
        today_expenses = 0
        week_income = 0
        week_expenses = 0
        month_income = 0
        month_expenses = 0
        
        for row in rows[1:]:
            if len(row) < 6:
                continue
            try:
                date_str = row[0]
                exp_date = datetime.strptime(date_str, "%d.%m.%Y")
                amount = float(row[5])
                
                if date_str == today:
                    if amount > 0:
                        today_income += amount
                    else:
                        today_expenses += abs(amount)
                
                if exp_date >= week_ago:
                    if amount > 0:
                        week_income += amount
                    else:
                        week_expenses += abs(amount)
                
                if exp_date >= month_ago:
                    if amount > 0:
                        month_income += amount
                    else:
                        month_expenses += abs(amount)
            except:
                pass
        
        msg = (
            f"⚡ <b>Быстрая статистика</b>\n\n"
            f"📅 <b>Сегодня:</b> +{format_amount(today_income)} / -{format_amount(today_expenses)} = {format_amount(today_income - today_expenses)}\n"
            f"📆 <b>Неделя:</b> +{format_amount(week_income)} / -{format_amount(week_expenses)} = {format_amount(week_income - week_expenses)}\n"
            f"📊 <b>Месяц:</b> +{format_amount(month_income)} / -{format_amount(month_expenses)} = {format_amount(month_income - month_expenses)}"
        )
        
        return msg

    def export_csv(self) -> str:
        """Экспортирует в CSV"""
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        
        output = StringIO()
        writer = csv.writer(output)
        writer.writerows(rows)
        
        return output.getvalue()

    def get_month_summary(self) -> str:
        """Сводка за месяц с анализом"""
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        
        current_month = datetime.now().strftime("%m.%Y")
        income = 0
        expenses_sum = {}
        total_expenses = 0
        
        for row in rows[1:]:
            if len(row) < 6 or not row[0].endswith(current_month):
                continue
            try:
                category = row[4]
                amount = float(row[5])
                
                if amount > 0:
                    income += amount
                else:
                    expenses_sum[category] = expenses_sum.get(category, 0) + abs(amount)
                    total_expenses += abs(amount)
            except:
                pass
        
        if income == 0 and total_expenses == 0:
            return "📈 Нет данных за этот месяц"
        
        msg = f"📈 <b>За месяц:</b>\n\n"
        if income > 0:
            msg += f"💚 <b>Доход:</b> +{format_amount(income)} сум\n"
        if total_expenses > 0:
            msg += f"📉 <b>Расходы:</b> -{format_amount(total_expenses)} сум\n"
        
        balance = income - total_expenses
        sign = "+" if balance >= 0 else ""
        msg += f"💰 <b>Баланс:</b> {sign}{format_amount(balance)} сум\n\n"
        
        msg += "<b>Расходы по категориям:</b>\n"
        for category in ["Быт", "Образование", "Инвестиции", "Reciprocity", "Долги"]:
            if category in expenses_sum:
                amount = expenses_sum[category]
                percent = (amount / total_expenses * 100) if total_expenses > 0 else 0
                cat_info = SYSTEM_CATEGORIES.get(category, {})
                emoji = cat_info.get("emoji", "💵")
                budget_percent = cat_info.get("percent", 0)
                
                bar = "🟩" * int(percent / 5) + "⬜" * (20 - int(percent / 5))
                status = "✅" if percent <= budget_percent else "⚠️"
                
                msg += (
                    f"{emoji} {status} {category}: {format_amount(amount)} сум ({percent:.0f}%)\n"
                    f"   {bar}\n"
                    f"   Бюджет: {budget_percent}%\n\n"
                )
        
        return msg

    def get_today_summary(self) -> str:
        """Сводка за сегодня"""
        ws = self.sheet.worksheet("Расходы")
        rows = ws.get_all_values()
        
        today = datetime.now().strftime("%d.%m.%Y")
        income = 0
        expenses_sum = {}
        total_expenses = 0
        
        for row in rows[1:]:
            if len(row) < 6 or row[0] != today:
                continue
            try:
                category = row[4]
                amount = float(row[5])
                
                if amount > 0:
                    income += amount
                else:
                    expenses_sum[category] = expenses_sum.get(category, 0) + abs(amount)
                    total_expenses += abs(amount)
            except:
                pass
        
        if income == 0 and total_expenses == 0:
            return "📊 Нет данных сегодня"
        
        msg = f"📊 <b>Сегодня:</b>\n\n"
        if income > 0:
            msg += f"💚 <b>Доход:</b> +{format_amount(income)} сум\n"
        if total_expenses > 0:
            msg += f"📉 <b>Расходы:</b> -{format_amount(total_expenses)} сум\n"
        
        balance = income - total_expenses
        sign = "+" if balance >= 0 else ""
        msg += f"💰 <b>Баланс:</b> {sign}{format_amount(balance)} сум"
        
        return msg


# ============================================================================
# ОБРАБОТЧИКИ КОМАНД
# ============================================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Команда /start"""
    text = (
        "👋 <b>Привет! Я трекер расходов с ИИ</b>\n\n"
        "📝 <b>Пиши что угодно:</b>\n"
        "• купил арбуз 50000\n"
        "• книга на курсе 85000\n"
        "• зарплата 8500000\n"
        "• кино с друзьями 100000 - весело было\n\n"
        "ИИ сам поймет категорию и подкатегорию!\n\n"
        "⚙️ <b>Команды:</b>\n"
        "/today — сегодня\n"
        "/quick — быстро\n"
        "/weekly — неделя\n"
        "/month — месяц\n"
        "/year — год\n"
        "/top — топ 5\n"
        "/trends — тренды\n"
        "/compare 01.2026 12.2025 — сравнить\n"
        "/undo — удалить\n"
        "/export — скачать\n"
        "/help — справка"
    )
    await update.message.reply_text(text, parse_mode=ParseMode.HTML)


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Обработчик текстовых сообщений"""
    tracker = context.bot_data.get('tracker')
    if not tracker:
        await update.message.reply_text("❌ Ошибка инициализации")
        return
    
    text = update.message.text.strip()
    result = tracker.parse_expense_ai(text)
    
    if not result:
        await update.message.reply_text(
            "❓ Не смог распарсить. Попробуй ещё раз:",
            parse_mode=ParseMode.HTML
        )
        return
    
    msg = tracker.write_expense(
        result['amount'],
        result['description'],
        result['category'],
        result['subcategory'],
        "",
        result.get('is_income', False)
    )
    
    await update.message.reply_text(f"✅\n\n{msg}", parse_mode=ParseMode.HTML)


async def today_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    msg = tracker.get_today_summary()
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def quick_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    msg = tracker.get_quick_stats()
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def weekly_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    msg = tracker.get_weekly_summary()
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def month_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    msg = tracker.get_month_summary()
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def year_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    msg = tracker.get_year_summary()
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def top_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    msg = tracker.get_top_expenses(5)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def trends_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    msg = tracker.get_trends()
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def compare_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    args = update.message.text.split()
    
    if len(args) < 3:
        await update.message.reply_text("Формат: /compare 01.2026 12.2025")
        return
    
    month1 = args[1]
    month2 = args[2]
    msg = tracker.compare_months(month1, month2)
    await update.message.reply_text(msg, parse_mode=ParseMode.HTML)


async def export_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tracker = context.bot_data.get('tracker')
    csv_data = tracker.export_csv()
    
    await update.message.reply_document(
        document=csv_data.encode(),
        filename="расходы.csv"
    )


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
    for idx, (row_idx, date, time, desc, subcat, cat, amount, comment) in enumerate(expenses, 1):
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
            row_idx, date, time, desc, subcat, cat, amount, comment = expenses[choice - 1]
            
            if tracker.delete_expense(row_idx):
                formatted_amt = format_amount(abs(amount))
                sign = "+" if amount > 0 else "-"
                await update.message.reply_text(
                    f"✅ Удалено!\n\n{sign}{formatted_amt} сум | {desc}",
                    parse_mode=ParseMode.HTML
                )
            else:
                await update.message.reply_text("❌ Ошибка")
        else:
            await update.message.reply_text(f"❌ От 1 до {len(expenses)}")
    except ValueError:
        await update.message.reply_text("❌ Напиши число")
    
    return ConversationHandler.END


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "ℹ️ <b>Справка</b>\n\n"
        "<b>Система категорий:</b>\n"
        "🏠 Быт 35% | 📚 Образование 10% | 💰 Инвестиции 10%\n"
        "❤️ Reciprocity 10% | 💳 Долги 35%\n\n"
        "<b>Ай просто поймёт:</b>\n"
        "• Арбуз, манго, яблоко → Еда и напитки\n"
        "• Парк, кино, театр → Развлечения\n"
        "• Зарплата, премия, доход → Доход\n"
        "• Помощь семье → Reciprocity\n\n"
        "<b>Форматы чисел:</b>\n"
        "5000, 5 000, 5.000, 5,000, 25k → все работают"
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
    
    if not os.path.exists(GOOGLE_CREDS):
        print("❌ credentials.json не найден")
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
    app.add_handler(CommandHandler("help", help_cmd))
    
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
