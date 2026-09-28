-- =====================================================================
-- Схема базы данных MVP «Поступление запасов»
-- Источник: PRD, раздел 9 «Модель данных».
--
-- Три правила, которые не обсуждаются (PRD, раздел 9):
--   1. Деньги хранятся только в numeric, никогда во float.
--   2. Ни одна строка регистра не существует без регистратора
--      (recorder_type + recorder_id).
--   3. Физического удаления документов нет — только пометка.
--
-- Все даты и время хранятся в UTC (timestamptz), показываются в Asia/Almaty.
-- =====================================================================

-- gen_random_uuid() встроена в PostgreSQL 13+.

-- ---------------------------------------------------------------------
-- 1. СПРАВОЧНИКИ (FR-01)
-- У каждого справочника есть код, наименование и пометка на удаление.
-- ---------------------------------------------------------------------

-- Организации (учреждения)
CREATE TABLE ref_organization (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code           varchar(20)  NOT NULL UNIQUE,
  name           varchar(255) NOT NULL,
  bin            varchar(12),                       -- БИН, 12 цифр
  deletion_mark  boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (bin IS NULL OR bin ~ '^[0-9]{12}$')
);

-- Контрагенты (поставщики)
CREATE TABLE ref_counterparty (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code           varchar(20)  NOT NULL UNIQUE,
  name           varchar(255) NOT NULL,
  bin            varchar(12),
  deletion_mark  boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (bin IS NULL OR bin ~ '^[0-9]{12}$')
);

-- Договоры. Договор всегда принадлежит одному контрагенту (FR-04).
CREATE TABLE ref_contract (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code             varchar(20)  NOT NULL UNIQUE,
  name             varchar(255) NOT NULL,           -- «№ 47 от 12.01.2026»
  org_id           uuid NOT NULL REFERENCES ref_organization,
  counterparty_id  uuid NOT NULL REFERENCES ref_counterparty,
  number           varchar(50),
  contract_date    date,
  deletion_mark    boolean NOT NULL DEFAULT false,
  created_at       timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX ix_contract_counterparty ON ref_contract (counterparty_id);

-- Единицы измерения
CREATE TABLE ref_uom (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code           varchar(20)  NOT NULL UNIQUE,
  name           varchar(100) NOT NULL,             -- «упак», «шт»
  deletion_mark  boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- Номенклатура.
-- kind заложен заранее (PRD, раздел 6): запасы / основное средство / услуга.
-- От него зависит, какое правило проводок сработает (раздел 10).
-- batch_tracked — признак партионности: в MVP не используется,
-- но заложен, чтобы потом не ломать схему.
CREATE TABLE ref_item (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code           varchar(20)  NOT NULL UNIQUE,
  name           varchar(255) NOT NULL,
  kind           varchar(20)  NOT NULL DEFAULT 'inventory'
                 CHECK (kind IN ('inventory', 'fixed_asset', 'service')),
  uom_id         uuid NOT NULL REFERENCES ref_uom,
  batch_tracked  boolean NOT NULL DEFAULT false,
  deletion_mark  boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- Склады
CREATE TABLE ref_warehouse (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code           varchar(20)  NOT NULL UNIQUE,
  name           varchar(255) NOT NULL,
  org_id         uuid NOT NULL REFERENCES ref_organization,
  deletion_mark  boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- Физические лица (в т.ч. МОЛ — материально ответственные лица)
CREATE TABLE ref_person (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code           varchar(20)  NOT NULL UNIQUE,
  name           varchar(255) NOT NULL,             -- ФИО
  iin            varchar(12),                       -- ИИН, 12 цифр
  deletion_mark  boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now(),
  CHECK (iin IS NULL OR iin ~ '^[0-9]{12}$')
);

-- План счетов. Номера счетов НИКОГДА не пишутся в коде — только здесь
-- и в правилах проводок (PRD, «О номерах счетов»).
CREATE TABLE ref_account (
  id             uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  code           varchar(10)  NOT NULL UNIQUE,      -- номер счёта: 1310, 3210…
  name           varchar(255) NOT NULL,
  deletion_mark  boolean NOT NULL DEFAULT false,
  created_at     timestamptz NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------------
-- 2. ДОКУМЕНТ «Поступление запасов» (слой 1)
-- ---------------------------------------------------------------------

-- Шапка документа
CREATE TABLE doc_goods_receipt (
  id               uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  number           varchar(20) NOT NULL,
  number_year      int NOT NULL,                    -- год нумерации по Asia/Almaty
  doc_date         timestamptz NOT NULL,
  org_id           uuid NOT NULL REFERENCES ref_organization,
  counterparty_id  uuid NOT NULL REFERENCES ref_counterparty,
  contract_id      uuid          REFERENCES ref_contract,
  warehouse_id     uuid NOT NULL REFERENCES ref_warehouse,
  mol_id           uuid          REFERENCES ref_person,
  supplier_doc_no  varchar(50),
  supplier_doc_date date,
  currency         char(3) NOT NULL DEFAULT 'KZT',   -- заложено заранее (раздел 6)
  amount_total     numeric(18,2) NOT NULL DEFAULT 0,
  status           varchar(12) NOT NULL DEFAULT 'draft'
                   CHECK (status IN ('draft', 'posted', 'deleted')),
  posted_at        timestamptz,
  author_id        uuid NOT NULL,
  created_at       timestamptz NOT NULL DEFAULT now(),
  UNIQUE (org_id, number_year, number)
);

-- Строки документа (табличная часть)
CREATE TABLE doc_goods_receipt_line (
  id       uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_id   uuid NOT NULL REFERENCES doc_goods_receipt ON DELETE CASCADE,
  line_no  int  NOT NULL,
  item_id  uuid NOT NULL REFERENCES ref_item,
  uom_id   uuid NOT NULL REFERENCES ref_uom,
  qty      numeric(18,3) NOT NULL CHECK (qty > 0),
  price    numeric(18,2) NOT NULL CHECK (price >= 0),
  amount   numeric(18,2) NOT NULL,
  UNIQUE (doc_id, line_no)
);

-- Нумерация документов (FR-03): отдельный счётчик на организацию, год и
-- тип документа. Счётчик увеличивается внутри той же транзакции, что и
-- запись документа, и блокирует свою строку — поэтому два пользователя
-- никогда не получат один номер, а при откате транзакции номер не
-- «сгорает» (нет разрывов). Обычная SEQUENCE так не умеет: она не
-- откатывается и оставляет дыры.
CREATE TABLE doc_number_counter (
  doc_type  varchar(40) NOT NULL,
  org_id    uuid NOT NULL REFERENCES ref_organization,
  year      int  NOT NULL,
  last_no   int  NOT NULL DEFAULT 0,
  PRIMARY KEY (doc_type, org_id, year)
);

-- Выдаёт следующий номер вида 'ПОС-000001'.
CREATE FUNCTION next_doc_number(p_doc_type varchar, p_org_id uuid,
                                p_year int, p_prefix varchar)
RETURNS varchar LANGUAGE plpgsql AS $$
DECLARE
  v_no int;
BEGIN
  INSERT INTO doc_number_counter (doc_type, org_id, year, last_no)
  VALUES (p_doc_type, p_org_id, p_year, 1)
  ON CONFLICT (doc_type, org_id, year)
  DO UPDATE SET last_no = doc_number_counter.last_no + 1
  RETURNING last_no INTO v_no;
  RETURN p_prefix || '-' || lpad(v_no::text, 6, '0');
END $$;

-- ---------------------------------------------------------------------
-- 3. РЕГИСТРЫ (слой 2). Заполняются ТОЛЬКО при проведении.
-- В каждой строке есть регистратор: recorder_type + recorder_id.
-- Отмена проведения = удалить все строки с этим регистратором.
-- ---------------------------------------------------------------------

-- Регистр накопления «Запасы» (остатки): сколько чего на складе
CREATE TABLE reg_stock (
  id             bigserial PRIMARY KEY,
  recorder_type  varchar(40) NOT NULL,              -- 'GoodsReceipt'
  recorder_id    uuid NOT NULL,
  line_no        int  NOT NULL,
  period         timestamptz NOT NULL,
  direction      smallint NOT NULL CHECK (direction IN (-1, 1)), -- +1 приход, -1 расход
  org_id         uuid NOT NULL REFERENCES ref_organization,
  warehouse_id   uuid NOT NULL REFERENCES ref_warehouse,
  item_id        uuid NOT NULL REFERENCES ref_item,
  mol_id         uuid          REFERENCES ref_person,
  qty            numeric(18,3) NOT NULL,
  amount         numeric(18,2) NOT NULL,
  UNIQUE (recorder_type, recorder_id, line_no)
);
CREATE INDEX ix_stock_slice ON reg_stock (org_id, warehouse_id, item_id, period);
CREATE INDEX ix_stock_rec   ON reg_stock (recorder_type, recorder_id);

-- Регистр накопления «Расчёты с поставщиками»: сколько мы должны
CREATE TABLE reg_settlement (
  id               bigserial PRIMARY KEY,
  recorder_type    varchar(40) NOT NULL,
  recorder_id      uuid NOT NULL,
  line_no          int  NOT NULL,
  period           timestamptz NOT NULL,
  direction        smallint NOT NULL CHECK (direction IN (-1, 1)), -- +1 долг вырос
  org_id           uuid NOT NULL REFERENCES ref_organization,
  counterparty_id  uuid NOT NULL REFERENCES ref_counterparty,
  contract_id      uuid          REFERENCES ref_contract,
  amount           numeric(18,2) NOT NULL,
  UNIQUE (recorder_type, recorder_id, line_no)
);
CREATE INDEX ix_settlement_slice ON reg_settlement (org_id, counterparty_id, contract_id, period);
CREATE INDEX ix_settlement_rec   ON reg_settlement (recorder_type, recorder_id);

-- Регистр сведений «Цены поставщиков» (периодический): последняя цена
-- на дату. Из него подставляется цена при вводе строки (FR-02).
CREATE TABLE reg_price (
  id               bigserial PRIMARY KEY,
  recorder_type    varchar(40) NOT NULL,
  recorder_id      uuid NOT NULL,
  line_no          int  NOT NULL,
  period           timestamptz NOT NULL,
  counterparty_id  uuid NOT NULL REFERENCES ref_counterparty,
  item_id          uuid NOT NULL REFERENCES ref_item,
  price            numeric(18,2) NOT NULL CHECK (price >= 0),
  UNIQUE (recorder_type, recorder_id, line_no)
);
CREATE INDEX ix_price_slice ON reg_price (counterparty_id, item_id, period DESC);
CREATE INDEX ix_price_rec   ON reg_price (recorder_type, recorder_id);

-- ---------------------------------------------------------------------
-- 4. ПРОВОДКИ (слой 3). Бухгалтерский регистр: одна строка = одна проводка.
-- ---------------------------------------------------------------------
CREATE TABLE acc_entry (
  id             bigserial PRIMARY KEY,
  recorder_type  varchar(40) NOT NULL,
  recorder_id    uuid NOT NULL,
  line_no        int  NOT NULL,
  period         timestamptz NOT NULL,
  org_id         uuid NOT NULL REFERENCES ref_organization,
  dt_account     varchar(10) NOT NULL REFERENCES ref_account (code),
  dt_dims        jsonb NOT NULL DEFAULT '{}',        -- аналитика (субконто) по дебету
  kt_account     varchar(10) NOT NULL REFERENCES ref_account (code),
  kt_dims        jsonb NOT NULL DEFAULT '{}',        -- аналитика по кредиту
  amount         numeric(18,2) NOT NULL CHECK (amount > 0),
  currency       char(3) NOT NULL DEFAULT 'KZT',
  content        varchar(255),
  UNIQUE (recorder_type, recorder_id, line_no)
);
CREATE INDEX ix_entry_dt  ON acc_entry (org_id, dt_account, period);
CREATE INDEX ix_entry_kt  ON acc_entry (org_id, kt_account, period);
CREATE INDEX ix_entry_rec ON acc_entry (recorder_type, recorder_id);

-- ---------------------------------------------------------------------
-- 5. МЕТАДАННЫЕ: правила проведения (FR-13).
-- Правило — это данные, а не код. Движок читает правила отсюда, поэтому
-- администратор меняет их без релиза и без перезапуска.
-- kind: 'register' — движение регистра, 'entry' — проводка.
-- sort_order: правила проверяются по порядку, применяется первое
-- подошедшее (PRD, раздел 10, «Частные случаи»).
-- ---------------------------------------------------------------------
CREATE TABLE meta_posting_rule (
  id          uuid PRIMARY KEY DEFAULT gen_random_uuid(),
  doc_type    varchar(40) NOT NULL,                  -- 'GoodsReceipt'
  kind        varchar(10) NOT NULL CHECK (kind IN ('register', 'entry')),
  sort_order  int NOT NULL,
  definition  jsonb NOT NULL,
  is_active   boolean NOT NULL DEFAULT true,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  UNIQUE (doc_type, kind, sort_order)
);

-- ---------------------------------------------------------------------
-- 6. ЖУРНАЛ ДЕЙСТВИЙ (FR-15, аудит). Только добавление, без правки.
-- ---------------------------------------------------------------------
CREATE TABLE audit_log (
  id         bigserial PRIMARY KEY,
  at         timestamptz NOT NULL DEFAULT now(),
  user_id    uuid NOT NULL,
  action     varchar(20) NOT NULL,                   -- create | update | post | unpost | mark
  object_type varchar(40) NOT NULL,                  -- 'GoodsReceipt', 'ref_item'…
  object_id  uuid NOT NULL,
  details    jsonb NOT NULL DEFAULT '{}'
);
CREATE INDEX ix_audit_object ON audit_log (object_type, object_id);

-- Журнал нельзя менять задним числом: запрещаем UPDATE и DELETE.
CREATE FUNCTION audit_log_readonly() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
  RAISE EXCEPTION 'Журнал действий доступен только на чтение';
END $$;
CREATE TRIGGER trg_audit_readonly BEFORE UPDATE OR DELETE ON audit_log
  FOR EACH ROW EXECUTE FUNCTION audit_log_readonly();
