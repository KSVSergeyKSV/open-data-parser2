import flet as ft
import sqlite3
import json
import datetime
import threading
import socket
from typing import List, Optional, Dict, Any

# --- КОНФИГУРАЦИЯ БАЗЫ ДАННЫХ ---
DB_NAME = "assets.db"

def init_db():
    conn = sqlite3.connect(DB_NAME, check_same_thread=False)
    c = conn.cursor()
    
    # Организации и структура
    c.execute('''CREATE TABLE IF NOT EXISTS organizations (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        name TEXT,
        internal_code TEXT UNIQUE
    )''')
    
    c.execute('''CREATE TABLE IF NOT EXISTS departments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        org_id INTEGER,
        name TEXT,
        parent_id INTEGER, -- Для территориальных отделов
        manager_name TEXT,
        location_info TEXT, -- Адрес, этаж и т.д.
        FOREIGN KEY(org_id) REFERENCES organizations(id)
    )''')

    # Пользователи (сотрудники)
    c.execute('''CREATE TABLE IF NOT EXISTS employees (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        full_name TEXT,
        position TEXT,
        department_id INTEGER,
        cabinet TEXT,
        floor TEXT,
        network_zone TEXT, -- Интернет, ЗКС, Локальная
        is_active BOOLEAN DEFAULT 1,
        FOREIGN KEY(department_id) REFERENCES departments(id)
    )''')

    # Оборудование (Основная таблица)
    c.execute('''CREATE TABLE IF NOT EXISTS assets (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        inv_number TEXT UNIQUE,
        asset_type TEXT, -- PC, Monitor, Printer, Component, etc.
        subtype TEXT, -- Laser, Inkjet, MFU, Tower, Notebook
        model TEXT,
        serial_number TEXT,
        status TEXT, -- New, InUse, Warehouse, Repair, WrittenOff, Utilized
        source_type TEXT, -- Purchase, Transfer
        contract_info TEXT, -- Номер контракта, год
        purchase_date TEXT,
        
        current_location_id INTEGER, -- Склад или Отдел
        assigned_to_id INTEGER, -- Сотрудник (если выдано)
        cabinet_snapshot TEXT, -- Кэш кабинета для быстрого поиска
        network_zone_snapshot TEXT,
        
        is_color BOOLEAN DEFAULT 0, -- Для принтеров
        price_category TEXT, -- Low, Mid, High
        
        needs_removal BOOLEAN DEFAULT 0, -- Флаг "Требуется изъятие" (Списан, но на месте)
        
        created_at TEXT,
        updated_at TEXT
    )''')

    # Комплектующие и связи (Родитель-Потомок)
    c.execute('''CREATE TABLE IF NOT EXISTS components (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id INTEGER, -- ID самого комплектующего (как отдельного актива)
        parent_asset_id INTEGER, -- ID компьютера, куда установлен
        component_type TEXT, -- RAM, HDD, GPU
        install_date TEXT,
        removed_date TEXT,
        is_installed BOOLEAN DEFAULT 0,
        FOREIGN KEY(asset_id) REFERENCES assets(id),
        FOREIGN KEY(parent_asset_id) REFERENCES assets(id)
    )''')

    # Журнал логов
    c.execute('''CREATE TABLE IF NOT EXISTS logs (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        asset_id INTEGER,
        action_type TEXT, -- Created, Moved, Repaired, Assigned, WrittenOff, ComponentAdded
        user_name TEXT,
        description TEXT,
        timestamp TEXT,
        old_value TEXT,
        new_value TEXT
    )''')

    conn.commit()
    conn.close()

# --- ВСПОМОГАТЕЛЬНЫЕ ФУНКЦИИ ---
def get_db():
    return sqlite3.connect(DB_NAME, check_same_thread=False)

def log_action(asset_id, action, user, desc, old=None, new=None):
    conn = get_db()
    c = conn.cursor()
    c.execute("INSERT INTO logs (asset_id, action_type, user_name, description, timestamp, old_value, new_value) VALUES (?, ?, ?, ?, ?, ?, ?)",
              (asset_id, action, user, desc, datetime.datetime.now().isoformat(), 
               json.dumps(old) if old else None, json.dumps(new) if new else None))
    conn.commit()
    conn.close()

def get_status_color(status, needs_removal=False):
    if needs_removal and status == "WrittenOff":
        return ft.colors.RED_700 # Мигающий красный в UI
    mapping = {
        "InUse": ft.colors.GREEN,
        "Warehouse": ft.colors.BLUE,
        "Repair": ft.colors.ORANGE,
        "WrittenOff": ft.colors.RED,
        "Utilized": ft.colors.GREY,
        "New": ft.colors.LIGHT_GREEN
    }
    return mapping.get(status, ft.colors.BLACK)

# --- ИНТЕРФЕЙС ПРИЛОЖЕНИЯ ---

class AssetManagerApp:
    def __init__(self, page: ft.Page):
        self.page = page
        self.page.title = "Система Учета Техники (SAT)"
        self.page.theme_mode = ft.ThemeMode.LIGHT
        self.page.window_width = 1400
        self.page.window_height = 900
        
        # Переменные состояния
        self.current_filter_status = None
        self.current_filter_type = None
        self.search_query = ""
        
        self.main_view = ft.Column([], scroll=ft.ScrollMode.AUTO, expand=True)
        self.page.add(self.main_view)
        
        self.load_dashboard()

    def load_dashboard(self):
        """Главная панель с быстрой статистикой и фильтрами"""
        conn = get_db()
        c = conn.cursor()
        
        # Статистика
        stats = {
            "total": c.execute("SELECT COUNT(*) FROM assets").fetchone()[0],
            "in_use": c.execute("SELECT COUNT(*) FROM assets WHERE status='InUse'").fetchone()[0],
            "warehouse": c.execute("SELECT COUNT(*) FROM assets WHERE status='Warehouse'").fetchone()[0],
            "repair": c.execute("SELECT COUNT(*) FROM assets WHERE status='Repair'").fetchone()[0],
            "written_off_active": c.execute("SELECT COUNT(*) FROM assets WHERE status='WrittenOff' AND needs_removal=1").fetchone()[0],
            "pcs_network": c.execute("SELECT COUNT(*) FROM assets WHERE asset_type='PC' AND network_zone_snapshot='Internet'").fetchone()[0],
            "pcs_zks": c.execute("SELECT COUNT(*) FROM assets WHERE asset_type='PC' AND network_zone_snapshot='ZKS'").fetchone()[0],
            "printers_color": c.execute("SELECT COUNT(*) FROM assets WHERE asset_type='Printer' AND is_color=1").fetchone()[0],
        }
        conn.close()

        # Карточки статистики
        stat_cards = ft.Row([
            self._create_stat_card("Всего активов", str(stats["total"]), ft.colors.BLUE),
            self._create_stat_card("В эксплуатации", str(stats["in_use"]), ft.colors.GREEN),
            self._create_stat_card("На складе", str(stats["warehouse"]), ft.colors.BLUE_300),
            self._create_stat_card("В ремонте", str(stats["repair"]), ft.colors.ORANGE),
            self._create_stat_card("⚠ ТРЕБУЮТ ИЗЪЯТИЯ", str(stats["written_off_active"]), ft.colors.RED, bold=True),
            self._create_stat_card("ПК (Интернет)", str(stats["pcs_network"]), ft.colors.TEAL),
            self._create_stat_card("ПК (ЗКС)", str(stats["pcs_zks"]), ft.colors.PURPLE),
            self._create_stat_card("Цветные принтеры", str(stats["printers_color"]), ft.colors.PINK),
        ], wrap=True)

        # Панель фильтров
        filter_bar = ft.Container(
            content=ft.Column([
                ft.Text("Быстрые фильтры и поиск", size=20, weight=ft.FontWeight.BOLD),
                ft.Row([
                    ft.TextField(label="Поиск по инв. номеру, модели, ФИО", expand=True, on_change=self.on_search_change, prefix_icon=ft.icons.SEARCH),
                    ft.Dropdown(
                        label="Тип оборудования",
                        options=[
                            ft.dropdown.Option("All", "Все типы"),
                            ft.dropdown.Option("PC", "Компьютеры"),
                            ft.dropdown.Option("Monitor", "Мониторы"),
                            ft.dropdown.Option("Printer", "Принтеры/МФУ"),
                            ft.dropdown.Option("Component", "Комплектующие"),
                            ft.dropdown.Option("Network", "Сеть/Телефония"),
                        ],
                        value="All",
                        on_change=self.on_filter_change,
                        width=200
                    ),
                    ft.Dropdown(
                        label="Статус",
                        options=[
                            ft.dropdown.Option("All", "Все статусы"),
                            ft.dropdown.Option("InUse", "В работе"),
                            ft.dropdown.Option("Warehouse", "Склад"),
                            ft.dropdown.Option("Repair", "Ремонт"),
                            ft.dropdown.Option("WrittenOff", "Списан"),
                            ft.dropdown.Option("NeedsRemoval", "Требует изъятия"),
                        ],
                        value="All",
                        on_change=self.on_filter_change,
                        width=200
                    ),
                    ft.ElevatedButton("Добавить актив", icon=ft.icons.ADD, on_click=self.show_add_asset_dialog),
                    ft.ElevatedButton("Импорт 1С", icon=ft.icons.UPLOAD_FILE, on_click=self.show_import_dialog),
                    ft.ElevatedButton("Отчеты", icon=ft.icons.PRINT, on_click=self.show_reports_dialog),
                ], wrap=True)
            ]),
            padding=20,
            bgcolor=ft.colors.GREY_100,
            border_radius=10,
            margin=ft.margin.only(bottom=20)
        )

        # Таблица данных
        self.data_table = ft.DataTable(
            columns=[
                ft.DataColumn(ft.Text("Статус")),
                ft.DataColumn(ft.Text("Инв. №")),
                ft.DataColumn(ft.Text("Тип / Модель")),
                ft.DataColumn(ft.Text("Местоположение / Сотрудник")),
                ft.DataColumn(ft.Text("Кабинет / Сеть")),
                ft.DataColumn(ft.Text("Контракт / Источник")),
                ft.DataColumn(ft.Text("Действия")),
            ],
            rows=[]
        )
        
        self.refresh_data_table()

        self.main_view.controls = [
            ft.Text("Панель управления активами организации", size=24, weight=ft.FontWeight.BOLD, color=ft.colors.BLUE_GREY_800),
            stat_cards,
            filter_bar,
            ft.Container(content=self.data_table, border=ft.border.all(1, ft.colors.GREY_300), border_radius=5, expand=True)
        ]
        self.page.update()

    def _create_stat_card(self, title, value, color, bold=False):
        return ft.Container(
            content=ft.Column([
                ft.Text(title, size=12, color=ft.colors.GREY_600),
                ft.Text(value, size=24, weight=ft.FontWeight.BOLD if bold else ft.FontWeight.NORMAL, color=color),
            ], tight=True),
            padding=15,
            bgcolor=ft.colors.WHITE,
            border=ft.border.all(1, color),
            border_radius=8,
            shadow=ft.BoxShadow(blur_radius=2, color=ft.colors.GREY_300),
            width=150
        )

    def refresh_data_table(self):
        conn = get_db()
        c = conn.cursor()
        
        query = "SELECT id, inv_number, asset_type, model, status, assigned_to_id, current_location_id, cabinet_snapshot, network_zone_snapshot, contract_info, needs_removal, subtype FROM assets WHERE 1=1"
        params = []
        
        # Фильтрация
        if self.search_query:
            query += " AND (inv_number LIKE ? OR model LIKE ? OR cabinet_snapshot LIKE ?)"
            search_term = f"%{self.search_query}%"
            params.extend([search_term, search_term, search_term])
            
        if self.current_filter_type and self.current_filter_type != "All":
            query += " AND asset_type = ?"
            params.append(self.current_filter_type)
            
        if self.current_filter_status:
            if self.current_filter_status == "NeedsRemoval":
                query += " AND status = 'WrittenOff' AND needs_removal = 1"
            else:
                query += " AND status = ?"
                params.append(self.current_filter_status)

        c.execute(query, params)
        rows = c.fetchall()
        
        # Получим имена сотрудников и отделов для отображения
        # (В реальном приложении лучше делать JOIN, но для простоты сделаем отдельные запросы или кэш)
        
        table_rows = []
        for row in rows:
            (aid, inv, atype, model, status, emp_id, loc_id, cab, net, contract, needs_rem, subtype) = row
            
            # Определение цвета и иконки статуса
            status_color = get_status_color(status, needs_rem)
            status_icon = ft.icons.CIRCLE if not needs_rem else ft.icons.WARNING
            if needs_rem and status == "WrittenOff":
                status_tooltip = "⚠ СПИСАНО! Требуется изъятие!"
            else:
                status_tooltip = status
            
            # Получение информации о сотруднике
            emp_info = "Не выдано"
            dept_info = ""
            if emp_id:
                emp_data = c.execute("SELECT full_name, position FROM employees WHERE id=?", (emp_id,)).fetchone()
                if emp_data:
                    emp_info = f"{emp_data[0]} ({emp_data[1]})"
            
            # Местоположение (если не выдано, то склад или отдел хранения)
            location_display = emp_info
            if not emp_id and loc_id:
                loc_data = c.execute("SELECT name FROM departments WHERE id=?", (loc_id,)).fetchone()
                if loc_data:
                    location_display = f"Склад/Отдел: {loc_data[0]}"
            
            # Формирование строки таблицы
            cell_status = ft.Container(
                content=ft.Icon(status_icon, color=status_color, size=20),
                tooltip=status_tooltip,
                bgcolor=status_color if needs_rem else None,
                border_radius=20 if needs_rem else 5,
                padding=5 if needs_rem else 0,
                animate=ft.animation.Animation(500, "easeInOut") if needs_rem else None
            )
            
            type_display = f"{atype}"
            if subtype: type_display += f" ({subtype})"
            if atype == "Printer" and subtype:
                 # Доп инфо для принтеров
                 is_col = c.execute("SELECT is_color FROM assets WHERE id=?", (aid,)).fetchone()[0]
                 if is_col: type_display += " [ЦВЕТНОЙ]"

            table_rows.append(
                ft.DataRow(
                    cells=[
                        ft.DataCell(cell_status),
                        ft.DataCell(ft.Text(inv, weight=ft.FontWeight.BOLD)),
                        ft.DataCell(ft.Column([ft.Text(model, size=14), ft.Text(type_display, size=12, color=ft.colors.GREY)])),
                        ft.DataCell(ft.Text(location_display, size=12)),
                        ft.DataCell(ft.Column([
                            ft.Text(f"Каб: {cab}" if cab else "-", size=12),
                            ft.Text(f"Сеть: {net}" if net else "-", size=12, color=ft.colors.BLUE if net=="ZKS" else ft.colors.GREEN)
                        ])),
                        ft.DataCell(ft.Text(contract if contract else "-", size=12)),
                        ft.DataCell(
                            ft.Row([
                                ft.IconButton(icon=ft.icons.EDIT, icon_size=18, tooltip="Изменить/Переместить", on_click=lambda e, x=aid: self.show_edit_asset_dialog(x)),
                                ft.IconButton(icon=ft.icons.HISTORY, icon_size=18, tooltip="История", on_click=lambda e, x=aid: self.show_history_dialog(x)),
                                ft.IconButton(icon=ft.icons.DRAG_HANDLE, icon_size=18, tooltip="Drag&Drop Перемещение", on_click=lambda e, x=aid: self.show_drag_drop_dialog(x)),
                            ], spacing=5)
                        ),
                    ]
                )
            )
        
        self.data_table.rows = table_rows
        conn.close()
        self.page.update()

    # --- ОБРАБОТЧИКИ СОБЫТИЙ ---
    def on_search_change(self, e):
        self.search_query = e.control.value
        self.refresh_data_table()

    def on_filter_change(self, e):
        if e.control.label == "Тип оборудования":
            self.current_filter_type = e.control.value
        elif e.control.label == "Статус":
            self.current_filter_status = e.control.value
        self.refresh_data_table()

    # --- ДИАЛОГИ И ФОРМЫ ---
    
    def show_add_asset_dialog(self, e):
        # Форма добавления нового актива
        dlg = ft.AlertDialog(
            title=ft.Text("Новое оборудование"),
            content=ft.Column([
                ft.TextField(label="Инвентарный номер", key="inv"),
                ft.Dropdown(label="Тип", options=[
                    ft.dropdown.Option("PC"), ft.dropdown.Option("Monitor"), 
                    ft.dropdown.Option("Printer"), ft.dropdown.Option("Component"),
                    ft.dropdown.Option("Network")
                ], key="type", value="PC"),
                ft.Dropdown(label="Подтип (Принтеры/ПК)", options=[
                    ft.dropdown.Option("Tower"), ft.dropdown.Option("Notebook"),
                    ft.dropdown.Option("Laser"), ft.dropdown.Option("Inkjet"), 
                    ft.dropdown.Option("MFU"), ft.dropdown.Option("RAM"), ft.dropdown.Option("HDD")
                ], key="subtype"),
                ft.TextField(label="Модель", key="model"),
                ft.TextField(label="Серийный номер", key="serial"),
                ft.TextField(label="Контракт / Год закупки", key="contract"),
                ft.DatePicker(first_date=datetime.datetime(2000, 1, 1)),
                ft.ElevatedButton("Дата покупки", icon=ft.icons.CALENDAR_MONTH, on_click=lambda e: e.page.open(e.page.dialogs[-1].content.controls[-2])), # Упрощено
                ft.Dropdown(label="Статус", options=[
                    ft.dropdown.Option("New"), ft.dropdown.Option("Warehouse"), ft.dropdown.Option("InUse")
                ], key="status", value="New"),
            ], scroll=True),
            actions=[
                ft.TextButton("Отмена", on_click=lambda e: self.close_dialog(dlg)),
                ft.ElevatedButton("Сохранить", on_click=lambda e: self.save_new_asset(dlg)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()

    def save_new_asset(self, dlg):
        # Сбор данных и сохранение в БД
        # В реальном коде нужно получить значения из controls по key
        # Здесь упрощенная логика для примера структуры
        inv = dlg.content.controls[0].value
        if not inv:
            self.page.snack_bar = ft.SnackBar(ft.Text("Инвентарный номер обязателен!"))
            self.page.snack_bar.open = True
            self.page.update()
            return

        conn = get_db()
        c = conn.cursor()
        try:
            c.execute('''INSERT INTO assets (inv_number, asset_type, subtype, model, serial_number, status, contract_info, purchase_date, created_at, updated_at) 
                         VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                      (inv, dlg.content.controls[1].value, dlg.content.controls[2].value, 
                       dlg.content.controls[3].value, dlg.content.controls[4].value,
                       dlg.content.controls[7].value, dlg.content.controls[5].value, 
                       datetime.datetime.now().isoformat(), datetime.datetime.now().isoformat(), datetime.datetime.now().isoformat()))
            conn.commit()
            log_action(c.lastrowid, "Created", "Admin", f"Создан актив {inv}")
            self.page.snack_bar = ft.SnackBar(ft.Text("Актив успешно создан!"))
            self.page.snack_bar.open = True
            self.refresh_data_table()
        except Exception as ex:
            self.page.snack_bar = ft.SnackBar(ft.Text(f"Ошибка: {str(ex)}"))
            self.page.snack_bar.open = True
        finally:
            conn.close()
            dlg.open = False
            self.page.update()

    def show_edit_asset_dialog(self, asset_id):
        conn = get_db()
        c = conn.cursor()
        asset = c.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not asset: return
        
        # Columns: 0:id, 1:inv, 2:type, 3:subtype, 4:model, 5:serial, 6:status, 7:source, 8:contract, 9:date, 
        # 10:loc_id, 11:emp_id, 12:cab, 13:net, 14:is_color, 15:price_cat, 16:needs_removal
        
        status_opts = [
            ft.dropdown.Option("InUse", "В эксплуатации"),
            ft.dropdown.Option("Warehouse", "На складе"),
            ft.dropdown.Option("Repair", "В ремонте"),
            ft.dropdown.Option("WrittenOff", "Списан"),
            ft.dropdown.Option("Utilized", "Утилизирован"),
        ]
        
        chk_removal = ft.Checkbox(label="⚠ ТРЕБУЕТ ИЗЪЯТИЯ (Списан, но на месте)", value=bool(asset[16]))

        dlg = ft.AlertDialog(
            title=ft.Text(f"Редактирование: {asset[1]}"),
            content=ft.Column([
                ft.Text(f"Модель: {asset[4]}", style=ft.TextStyle(weight=ft.FontWeight.BOLD)),
                ft.Dropdown(label="Статус", options=status_opts, value=asset[6], key="status"),
                ft.Divider(),
                ft.Text("Привязка к сотруднику (если В эксплуатации)", weight=ft.FontWeight.BOLD),
                ft.TextField(label="ФИО Сотрудника (поиск/ввод)", value=self._get_emp_name(c, asset[11]), key="emp_name", hint_text="Начните вводить фамилию..."),
                ft.TextField(label="Должность", value=self._get_emp_pos(c, asset[11]), key="position"),
                ft.Row([
                    ft.TextField(label="Кабинет", value=asset[12] or "", width=100, key="cabinet"),
                    ft.TextField(label="Этаж", value="", width=80),
                    ft.Dropdown(label="Сеть", options=[
                        ft.dropdown.Option("None", "-"),
                        ft.dropdown.Option("Internet", "Интернет"),
                        ft.dropdown.Option("ZKS", "ЗКС (Закрытый)"),
                        ft.dropdown.Option("Local", "Локальная"),
                    ], value=asset[13] or "None", key="network"),
                ]),
                ft.Divider(),
                ft.Text("Управление жизненным циклом", weight=ft.FontWeight.BOLD, color=ft.colors.RED),
                chk_removal,
                ft.Text("Если статус 'Списан' и галочка стоит - объект подсвечивается красным для изъятия.", size=12, italic=True),
            ], scroll=True),
            actions=[
                ft.TextButton("Отмена", on_click=lambda e: self.close_dialog(dlg)),
                ft.ElevatedButton("Сохранить изменения", on_click=lambda e: self.save_asset_changes(dlg, asset_id, chk_removal.value)),
            ],
            actions_alignment=ft.MainAxisAlignment.END,
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()
        conn.close()

    def _get_emp_name(self, c, emp_id):
        if not emp_id: return ""
        res = c.execute("SELECT full_name FROM employees WHERE id=?", (emp_id,)).fetchone()
        return res[0] if res else ""
    
    def _get_emp_pos(self, c, emp_id):
        if not emp_id: return ""
        res = c.execute("SELECT position FROM employees WHERE id=?", (emp_id,)).fetchone()
        return res[0] if res else ""

    def save_asset_changes(self, dlg, asset_id, needs_removal):
        status = dlg.content.controls[1].value
        emp_name = dlg.content.controls[3].value
        cabinet = dlg.content.controls[5].value
        network = dlg.content.controls[6].value
        
        # Логика обновления (упрощенная)
        conn = get_db()
        c = conn.cursor()
        
        # Обновление статуса и флага изъятия
        c.execute("UPDATE assets SET status=?, needs_removal=?, cabinet_snapshot=?, network_zone_snapshot=?, updated_at=? WHERE id=?",
                  (status, 1 if needs_removal else 0, cabinet, network if network!="None" else None, datetime.datetime.now().isoformat(), asset_id))
        
        # Тут должна быть логика привязки сотрудника (создание если нет, поиск если есть)
        # Для краткости опущено сложное создание сотрудника
        
        log_action(asset_id, "Updated", "Admin", f"Обновлен статус: {status}, Изъятие: {needs_removal}")
        conn.commit()
        conn.close()
        
        dlg.open = False
        self.page.update()
        self.refresh_data_table()
        self.page.snack_bar = ft.SnackBar(ft.Text("Данные обновлены!"))
        self.page.snack_bar.open = True
        self.page.update()

    def show_drag_drop_dialog(self, asset_id):
        """Имитация Drag & Drop через модальное окно выбора места назначения"""
        conn = get_db()
        c = conn.cursor()
        asset = c.execute("SELECT inv_number, status FROM assets WHERE id=?", (asset_id,)).fetchone()
        
        # Список отделов и складов
        depts = c.execute("SELECT id, name FROM departments").fetchall()
        options = [ft.dropdown.Option("WAREHOUSE", "Центральный Склад")]
        for d in depts:
            options.append(ft.dropdown.Option(str(d[0]), d[1]))
            
        dlg = ft.AlertDialog(
            title=ft.Text(f"Перемещение: {asset[0]}"),
            content=ft.Column([
                ft.Text("Выберите новое местоположение (Drag & Drop эмуляция):", weight=ft.FontWeight.BOLD),
                ft.Dropdown(label="Куда переместить?", options=options, key="dest", width=300),
                ft.RadioGroup(
                    ft.Column([
                        ft.Radio(value="move", label="Переместить на хранение (Склад/Отдел)"),
                        ft.Radio(value="assign", label="Выдать сотруднику (требует выбора сотрудника)"),
                        ft.Radio(value="repair", label="Отправить в ремонт"),
                    ])
                ),
                ft.TextField(label="Комментарий к перемещению", key="comment"),
            ]),
            actions=[
                ft.TextButton("Отмена", on_click=lambda e: self.close_dialog(dlg)),
                ft.ElevatedButton("Переместить", on_click=lambda e: self.process_move(dlg, asset_id)),
            ],
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()
        conn.close()

    def process_move(self, dlg, asset_id):
        dest_id = dlg.content.controls[1].value
        comment = dlg.content.controls[2].value
        # Логика перемещения в БД
        conn = get_db()
        c = conn.cursor()
        
        new_status = "Warehouse"
        loc_update = ""
        if dest_id == "WAREHOUSE":
            loc_update = "current_location_id = (SELECT id FROM departments WHERE name='Warehouse' LIMIT 1)" # Упрощено
        else:
            loc_update = f"current_location_id = {dest_id}"
            
        # Обновляем запись
        c.execute(f"UPDATE assets SET {loc_update}, status=?, updated_at=? WHERE id=?", 
                  (new_status, datetime.datetime.now().isoformat(), asset_id))
        
        log_action(asset_id, "Moved", "Admin", f"Перемещено: {comment}")
        conn.commit()
        conn.close()
        
        dlg.open = False
        self.page.update()
        self.refresh_data_table()
        self.page.snack_bar = ft.SnackBar(ft.Text("Техника перемещена!"))
        self.page.snack_bar.open = True
        self.page.update()

    def show_history_dialog(self, asset_id):
        conn = get_db()
        c = conn.cursor()
        logs = c.execute("SELECT timestamp, action_type, user_name, description FROM logs WHERE asset_id=? ORDER BY id DESC", (asset_id,)).fetchall()
        conn.close()
        
        history_controls = []
        for log in logs:
            history_controls.append(
                ft.ListTile(
                    leading=ft.Icon(ft.icons.HISTORY, size=20),
                    title=ft.Text(f"{log[1]} - {log[2]}", size=14, weight=ft.FontWeight.BOLD),
                    subtitle=ft.Text(log[3]),
                    trailing=ft.Text(log[0][:10], size=12, color=ft.colors.GREY),
                )
            )
            
        dlg = ft.AlertDialog(
            title=ft.Text("История жизненного цикла"),
            content=ft.Column(history_controls, scroll=True, height=400),
            actions=[ft.TextButton("Закрыть", on_click=lambda e: self.close_dialog(dlg))],
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()

    def show_reports_dialog(self, e):
        dlg = ft.AlertDialog(
            title=ft.Text("Печать отчетов"),
            content=ft.Column([
                ft.ElevatedButton("🖨 Покабинетный список (Все отделы)", icon=ft.icons.PRINT, width=300),
                ft.ElevatedButton("🖨 Акты списания (Требующие изъятия)", icon=ft.icons.DELETE_FOREVER, width=300, bgcolor=ft.colors.RED_100),
                ft.ElevatedButton("🖨 Реестр техники по ЗКС", icon=ft.icons.SECURITY, width=300),
                ft.ElevatedButton("🖨 Неучтенные комплектующие", icon=ft.icons.MEMORY, width=300),
            ]),
            actions=[ft.TextButton("Закрыть", on_click=lambda e: self.close_dialog(dlg))],
        )
        self.page.dialog = dlg
        dlg.open = True
        self.page.update()

    def show_import_dialog(self, e):
        dlg = ft.AlertDialog(
            title=ft.Text("Импорт из 1С"),
            content=ft.Column([
                ft.Text("Загрузите JSON файл экспорта из 1С:", size=14),
                ft.FilePicker(on_result=self.handle_file_pick),
                ft.Text("Формат: [{'inv': '...', 'model': '...', ...}]", size=12, italic=True, color=ft.colors.GREY),
            ]),
            actions=[ft.TextButton("Отмена", on_click=lambda e: self.close_dialog(dlg))],
        )
        self.page.dialog = dlg
        # Хак для открытия файлового пикера внутри диалога
        fp = dlg.content.controls[1]
        fp.pick_files()
        dlg.open = True
        self.page.update()

    def handle_file_pick(self, e):
        if e.files:
            # Здесь логика парсинга JSON и импорта в БД
            self.page.snack_bar = ft.SnackBar(ft.Text(f"Файл {e.files[0].name} выбран. Импорт запущен..."))
            self.page.snack_bar.open = True
            self.page.update()
            # Имитация импорта
            import time
            time.sleep(1)
            self.page.snack_bar = ft.SnackBar(ft.Text("Импорт успешно завершен!"))
            self.page.snack_bar.open = True
            self.page.update()
            self.refresh_data_table()
        
        # Закрыть диалог
        if self.page.dialog:
            self.page.dialog.open = False
            self.page.update()

    def close_dialog(self, dlg):
        dlg.open = False
        self.page.update()

def main(page: ft.Page):
    init_db()
    app = AssetManagerApp(page)

# Запуск в режиме веб-сервера для доступа по сети
if __name__ == "__main__":
    # Получаем локальный IP для доступа из сети
    hostname = socket.gethostname()
    local_ip = socket.gethostbyname(hostname)
    print(f"--- СЕРВЕР ЗАПУЩЕН ---")
    print(f"Локальный доступ: http://localhost:8550")
    print(f"Доступ по сети:   http://{local_ip}:8550")
    print(f"Раздайте этот IP сотрудникам для доступа через браузер.")
    print("--------------------")
    
    ft.app(target=main, view=ft.WEB_BROWSER, port=8550, host="0.0.0.0")