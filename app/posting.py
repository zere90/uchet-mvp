"""Проведение поступлений по правилам из базы данных."""
import re
from decimal import Decimal, ROUND_HALF_UP, localcontext
from uuid import UUID

import psycopg
from psycopg import errors as pg, sql
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from app.errors import PostingError, detail

DOC_TYPE = "GoodsReceipt"
# Только этот словарь знает таблицы и колонки регистров.
REGISTERS = {
    "reg_stock": {
        "table": "reg_stock", "org": True, "direction": True,
        "dims": {"warehouse": "warehouse_id", "item": "item_id", "mol": "mol_id"},
        "res": {"qty": "qty", "amount": "amount"},
    },
    "reg_settlement": {
        "table": "reg_settlement", "org": True, "direction": True,
        "dims": {"counterparty": "counterparty_id", "contract": "contract_id"},
        "res": {"amount": "amount"},
    },
    "reg_price": {
        "table": "reg_price", "org": False, "direction": False,
        "dims": {"counterparty": "counterparty_id", "item": "item_id"},
        "res": {"price": "price"},
    },
}
PATH = r"(?:doc|line)\.[a-z_]+(?:\.[a-z_]+)*"
CONDITION = re.compile(rf"\s*({PATH})\s*==\s*'([^']*)'\s*")


def _fail(field, code, message):
    raise PostingError([detail(field, code, message)])


def _path(path, doc, line):
    if not isinstance(path, str) or not re.fullmatch(PATH, path):
        raise ValueError("Недопустимый путь в правиле")
    root, *parts = path.split(".")
    value = doc if root == "doc" else line
    for part in parts:
        if not isinstance(value, dict):
            raise ValueError("Путь не соответствует данным документа")
        key = part if part in value else part + "_id"
        value = value[key]
    return value["id"] if isinstance(value, dict) else value


def _matches(rule, doc, line):
    if "when" not in rule:
        return True
    match = CONDITION.fullmatch(rule["when"])
    if match is None:
        raise ValueError("Условие должно иметь вид путь == 'значение'")
    return _path(match[1], doc, line) == match[2]


def _dims(paths, doc, line):
    return Jsonb({p.rsplit(".", 1)[-1]: str(value) if isinstance(value, UUID) else value
                  for p in paths for value in [_path(p, doc, line)]})


def _lock(cur, doc_id, *, nowait):
    cur.execute("SELECT * FROM doc_goods_receipt WHERE id = %s FOR UPDATE"
                + (" NOWAIT" if nowait else ""), (doc_id,))
    doc = cur.fetchone()
    if doc is None:
        _fail("id", "not_found", "Документ не найден")
    return doc


def _validate(cur, doc, lines):
    if doc["status"] == "deleted":
        _fail("status", "deleted", "Документ помечен на удаление")
    problems = []
    for field, title in (("org_id", "Организация"), ("counterparty_id", "Поставщик"),
                         ("warehouse_id", "Склад")):
        if doc[field] is None:
            problems.append(detail(field, "required", f"Не заполнен реквизит «{title}»"))
    if not lines:
        problems.append(detail("lines", "empty", "Добавьте хотя бы одну строку документа"))
    if doc["contract_id"] is not None:
        cur.execute("SELECT org_id, counterparty_id FROM ref_contract WHERE id = %s",
                    (doc["contract_id"],))
        contract = cur.fetchone()
        if contract is None or any(contract[k] != doc[k] for k in ("org_id", "counterparty_id")):
            problems.append(detail("contract_id", "contract_mismatch",
                                   "Договор должен принадлежать выбранным поставщику и организации"))
    with localcontext() as context:
        context.prec = 50
        total = Decimal("0.00")
        for index, line in enumerate(lines):
            prefix, number = f"lines[{index}]", line["line_no"]
            for field, valid, code, message in (
                ("qty", line["qty"].is_finite() and line["qty"] > 0, "must_be_positive",
                 f"Количество в строке {number} должно быть больше нуля"),
                ("price", line["price"].is_finite() and line["price"] >= 0, "must_be_nonnegative",
                 f"Цена в строке {number} не может быть отрицательной"),
            ):
                if not valid:
                    problems.append(detail(f"{prefix}.{field}", code, message))
            if line["qty"].is_finite() and line["price"].is_finite():
                expected = (line["qty"] * line["price"]).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
                if line["amount"] != expected:
                    problems.append(detail(f"{prefix}.amount", "amount_mismatch",
                                           f"Сумма в строке {number} должна равняться количеству × цене с округлением до копеек"))
            total += line["amount"]
        if not total.is_finite() or total != doc["amount_total"]:
            problems.append(detail("amount_total", "total_mismatch",
                                   "Сумма документа должна равняться сумме округлённых сумм строк"))
    if problems:
        raise PostingError(problems)


def _clear(cur, doc_id):
    for table in [r["table"] for r in REGISTERS.values()] + ["acc_entry"]:
        cur.execute(sql.SQL("DELETE FROM {} WHERE recorder_type = %s AND recorder_id = %s")
                    .format(sql.Identifier(table)), (DOC_TYPE, doc_id))


def _base(doc, number):
    return {"recorder_type": DOC_TYPE, "recorder_id": doc["id"],
            "line_no": number, "period": doc["doc_date"]}


def _build(doc, lines, rules):
    registers = {name: [] for name in REGISTERS}
    entries, matched = [], set()
    for stored in rules:
        rule = stored["definition"]
        try:
            scope = rule["for_each"]
            if scope not in ("lines", "document") or (stored["kind"] == "entry" and scope != "lines"):
                raise ValueError("Неверная область применения правила")
            for index, line in enumerate(lines if scope == "lines" else [None]):
                if stored["kind"] == "entry" and index in matched:
                    continue
                if not _matches(rule, doc, line):
                    continue
                if stored["kind"] == "register":
                    name = rule["register"]
                    spec, rows = REGISTERS[name], registers[name]
                    row = _base(doc, len(rows) + 1)
                    if spec["org"]:
                        row["org_id"] = doc["org_id"]
                    if spec["direction"]:
                        if rule["direction"] not in ("+1", "-1", 1, -1):
                            raise ValueError("Неверное направление движения")
                        row["direction"] = int(rule["direction"])
                    for group in ("dims", "res"):
                        if rule[group].keys() != spec[group].keys():
                            raise ValueError("Неверный состав измерений или ресурсов")
                        for key, path in rule[group].items():
                            row[spec[group][key]] = _path(path, doc, line)
                    rows.append(row)
                else:
                    row = {**_base(doc, len(entries) + 1), "org_id": doc["org_id"],
                           "currency": doc["currency"], "amount": _path(rule["amount"], doc, line)}
                    if not isinstance(row["amount"], Decimal) or not row["amount"].is_finite() or row["amount"] < 0:
                        raise ValueError("Сумма проводки должна быть неотрицательной денежной суммой")
                    for side, prefix in (("debit", "dt"), ("credit", "kt")):
                        row[prefix + "_account"] = rule[side]["account"]
                        row[prefix + "_dims"] = _dims(rule[side].get("dims", []), doc, line)
                    matched.add(index)
                    entries.append(row)
        except (KeyError, TypeError, ValueError, AttributeError) as exc:
            _fail("rules", "invalid_rule", f"Ошибка в правиле проведения № {stored['sort_order']}: {exc}")
    missing = [detail(f"lines[{i}]", "no_entry_rule", f"нет правила проводок для строки {line['line_no']}")
               for i, line in enumerate(lines) if i not in matched]
    if missing:
        raise PostingError(missing)
    return registers, entries


def _insert(cur, table, row):
    cur.execute(sql.SQL("INSERT INTO {} ({}) VALUES ({})").format(
        sql.Identifier(table), sql.SQL(", ").join(map(sql.Identifier, row)),
        sql.SQL(", ").join(sql.Placeholder() for _ in row)), list(row.values()))


def _audit(cur, doc_id, user_id, action):
    cur.execute("INSERT INTO audit_log (user_id, action, object_type, object_id) VALUES (%s, %s, %s, %s)",
                (user_id, action, DOC_TYPE, doc_id))


def post(conn: psycopg.Connection, doc_id: UUID, user_id: UUID) -> None:
    """Провести атомарно; внутри внешней транзакции используется точка сохранения."""
    try:
        with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
            doc = _lock(cur, doc_id, nowait=True)
            cur.execute("SELECT l.*, jsonb_build_object('id', i.id::text, 'kind', i.kind) AS item "
                        "FROM doc_goods_receipt_line l JOIN ref_item i ON i.id = l.item_id "
                        "WHERE l.doc_id = %s ORDER BY l.line_no", (doc_id,))
            lines = cur.fetchall()
            _validate(cur, doc, lines)
            _clear(cur, doc_id)
            cur.execute("SELECT * FROM meta_posting_rule WHERE doc_type = %s AND is_active "
                        "ORDER BY sort_order, kind", (DOC_TYPE,))
            registers, entries = _build(doc, lines, cur.fetchall())
            if sum((e["amount"] for e in entries), Decimal("0.00")) != doc["amount_total"]:
                _fail("amount_total", "entries_total_mismatch", "Сумма проводок не совпадает с суммой документа")
            accounts = {e[side] for e in entries for side in ("dt_account", "kt_account")}
            cur.execute("SELECT code FROM ref_account WHERE code = ANY(%s) AND NOT deletion_mark FOR SHARE",
                        (list(accounts),))
            missing = accounts - {r["code"] for r in cur.fetchall()}
            if missing:
                _fail("accounts", "unavailable_account", "Счёт отсутствует или помечен на удаление: " + ", ".join(sorted(missing)))
            for name, rows in registers.items():
                for row in rows:
                    _insert(cur, REGISTERS[name]["table"], row)
            # Бесплатные строки не создают нулевых бухгалтерских проводок.
            for number, row in enumerate((e for e in entries if e["amount"] != 0), 1):
                row["line_no"] = number
                _insert(cur, "acc_entry", row)
            cur.execute("UPDATE doc_goods_receipt SET status = 'posted', posted_at = now() WHERE id = %s", (doc_id,))
            _audit(cur, doc_id, user_id, "post")
    except pg.LockNotAvailable:
        _fail("id", "document_busy", "Документ занят другим пользователем")


def unpost(conn: psycopg.Connection, doc_id: UUID, user_id: UUID) -> None:
    """Отменить проведение и записать действие в журнал в одной транзакции."""
    with conn.transaction(), conn.cursor(row_factory=dict_row) as cur:
        _lock(cur, doc_id, nowait=False)
        _clear(cur, doc_id)
        cur.execute("UPDATE doc_goods_receipt SET status = 'draft', posted_at = NULL WHERE id = %s", (doc_id,))
        _audit(cur, doc_id, user_id, "unpost")
