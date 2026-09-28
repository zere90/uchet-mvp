-- =====================================================================
-- Тестовые данные из примера в PRD: накладная ПОС-000001 от 14.09.2026.
-- UUID заданы вручную, чтобы их было удобно использовать в запросах API.
-- =====================================================================

-- Организация
INSERT INTO ref_organization (id, code, name, bin) VALUES
  ('00000000-0000-0000-0000-000000000001', 'ORG-001',
   'ГУ «Отдел образования г. Астаны»', NULL);

-- Поставщик
INSERT INTO ref_counterparty (id, code, name, bin) VALUES
  ('00000000-0000-0000-0000-000000000101', 'KA-001',
   'ТОО «Алматы Канцтовары»', '990340000123');

-- Договор с поставщиком
INSERT INTO ref_contract (id, code, name, org_id, counterparty_id, number, contract_date) VALUES
  ('00000000-0000-0000-0000-000000000201', 'DOG-047', '№ 47 от 12.01.2026',
   '00000000-0000-0000-0000-000000000001',
   '00000000-0000-0000-0000-000000000101', '47', '2026-01-12');

-- Склад
INSERT INTO ref_warehouse (id, code, name, org_id) VALUES
  ('00000000-0000-0000-0000-000000000301', 'SKL-001', 'Центральный склад',
   '00000000-0000-0000-0000-000000000001');

-- Материально ответственное лицо
INSERT INTO ref_person (id, code, name) VALUES
  ('00000000-0000-0000-0000-000000000401', 'FL-001', 'Жумабаева А. К.');

-- Единицы измерения
INSERT INTO ref_uom (id, code, name) VALUES
  ('00000000-0000-0000-0000-000000000501', 'UPAK', 'упак'),
  ('00000000-0000-0000-0000-000000000502', 'SHT',  'шт');

-- Номенклатура: три позиции из накладной + ноутбук и услуга для «тренажёра»
INSERT INTO ref_item (id, code, name, kind, uom_id) VALUES
  ('00000000-0000-0000-0000-000000000601', 'NOM-001', 'Бумага А4 SvetoCopy, 500 л.', 'inventory',
   '00000000-0000-0000-0000-000000000501'),
  ('00000000-0000-0000-0000-000000000602', 'NOM-002', 'Картридж HP CF283A', 'inventory',
   '00000000-0000-0000-0000-000000000502'),
  ('00000000-0000-0000-0000-000000000603', 'NOM-003', 'Ручка шариковая синяя', 'inventory',
   '00000000-0000-0000-0000-000000000502'),
  ('00000000-0000-0000-0000-000000000604', 'NOM-004', 'Ноутбук', 'fixed_asset',
   '00000000-0000-0000-0000-000000000502'),
  ('00000000-0000-0000-0000-000000000605', 'NOM-005', 'Заправка картриджа', 'service',
   '00000000-0000-0000-0000-000000000502');

-- План счетов.
-- ВНИМАНИЕ: номера счетов — иллюстрация из PRD. Точные субсчета нужно
-- сверить с приложением к приказу Министра финансов РК от 16.04.2025 № 170
-- (открытый вопрос к бухгалтеру-эксперту, PRD раздел 14).
INSERT INTO ref_account (code, name) VALUES
  ('1080', 'Деньги на счёте'),
  ('1310', 'Запасы'),
  ('2410', 'Основные средства'),
  ('3210', 'Расчёты с поставщиками (кредиторская задолженность)'),
  ('7010', 'Расходы учреждения');

-- ---------------------------------------------------------------------
-- Правила проведения документа «Поступление запасов» (PRD, раздел 10)
-- ---------------------------------------------------------------------

-- Движения регистров
INSERT INTO meta_posting_rule (doc_type, kind, sort_order, definition) VALUES
  ('GoodsReceipt', 'register', 10, '{
     "register": "reg_stock", "for_each": "lines",
     "when": "line.item.kind == ''inventory''", "direction": "+1",
     "dims": {"warehouse": "doc.warehouse", "item": "line.item", "mol": "doc.mol"},
     "res":  {"qty": "line.qty", "amount": "line.amount"}
   }'),
  ('GoodsReceipt', 'register', 20, '{
     "register": "reg_settlement", "for_each": "document", "direction": "+1",
     "dims": {"counterparty": "doc.counterparty", "contract": "doc.contract"},
     "res":  {"amount": "doc.amount_total"}
   }'),
  ('GoodsReceipt', 'register', 30, '{
     "register": "reg_price", "for_each": "lines",
     "dims": {"counterparty": "doc.counterparty", "item": "line.item"},
     "res":  {"price": "line.price"}
   }');

-- Проводки
INSERT INTO meta_posting_rule (doc_type, kind, sort_order, definition) VALUES
  ('GoodsReceipt', 'entry', 10, '{
     "for_each": "lines", "when": "line.item.kind == ''inventory''",
     "debit":  {"account": "1310", "dims": ["doc.warehouse", "line.item", "doc.mol"]},
     "credit": {"account": "3210", "dims": ["doc.counterparty", "doc.contract"]},
     "amount": "line.amount"
   }'),
  ('GoodsReceipt', 'entry', 20, '{
     "for_each": "lines", "when": "line.item.kind == ''fixed_asset''",
     "debit":  {"account": "2410"},
     "credit": {"account": "3210", "dims": ["doc.counterparty", "doc.contract"]},
     "amount": "line.amount"
   }'),
  ('GoodsReceipt', 'entry', 30, '{
     "for_each": "lines", "when": "line.item.kind == ''service''",
     "debit":  {"account": "7010"},
     "credit": {"account": "3210", "dims": ["doc.counterparty", "doc.contract"]},
     "amount": "line.amount"
   }');
