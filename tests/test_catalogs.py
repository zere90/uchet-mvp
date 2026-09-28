"""Проверки справочников (FR-01): CRUD, поиск, пометка на удаление, ошибки."""

ORG = "00000000-0000-0000-0000-000000000001"
SUPPLIER = "00000000-0000-0000-0000-000000000101"
UOM_SHT = "00000000-0000-0000-0000-000000000502"


def test_seed_data_is_loaded(client):
    r = client.get("/api/v1/catalogs/items")
    assert r.status_code == 200
    names = [i["name"] for i in r.json()["items"]]
    assert "Картридж HP CF283A" in names
    assert r.json()["total"] == 5


def test_all_eight_catalogs_exist(client):
    for name in ["organizations", "counterparties", "contracts", "units",
                 "items", "warehouses", "persons", "accounts"]:
        assert client.get(f"/api/v1/catalogs/{name}").status_code == 200, name


def test_create_get_update(client):
    r = client.post("/api/v1/catalogs/items", json={
        "code": "NOM-100", "name": "Скрепки 28 мм", "uom_id": UOM_SHT})
    assert r.status_code == 201
    item = r.json()
    assert item["kind"] == "inventory" and item["deletion_mark"] is False

    assert client.get(f"/api/v1/catalogs/items/{item['id']}").json()["name"] == "Скрепки 28 мм"

    r = client.put(f"/api/v1/catalogs/items/{item['id']}", json={
        "code": "NOM-100", "name": "Скрепки 50 мм", "uom_id": UOM_SHT})
    assert r.status_code == 200
    assert r.json()["name"] == "Скрепки 50 мм"


def test_search_by_name_and_code(client):
    r = client.get("/api/v1/catalogs/items", params={"q": "картридж"})
    assert [i["code"] for i in r.json()["items"]] == ["NOM-002", "NOM-005"]  # картридж + заправка картриджа
    r = client.get("/api/v1/catalogs/items", params={"q": "NOM-003"})
    assert [i["name"] for i in r.json()["items"]] == ["Ручка шариковая синяя"]


def test_deletion_mark_hides_but_keeps_row(client, db):
    item_id = "00000000-0000-0000-0000-000000000603"
    r = client.post(f"/api/v1/catalogs/items/{item_id}/deletion-mark", json={"mark": True})
    assert r.status_code == 200 and r.json()["deletion_mark"] is True

    listed = [i["id"] for i in client.get("/api/v1/catalogs/items").json()["items"]]
    assert item_id not in listed
    with_deleted = client.get("/api/v1/catalogs/items", params={"include_deleted": True}).json()
    assert item_id in [i["id"] for i in with_deleted["items"]]
    # физически строка осталась в базе
    assert db.execute("SELECT count(*) AS n FROM ref_item WHERE id = %s", (item_id,)).fetchone()["n"] == 1


def test_no_physical_delete_endpoint(client):
    r = client.delete("/api/v1/catalogs/items/00000000-0000-0000-0000-000000000603")
    assert r.status_code == 405


def test_duplicate_code_is_readable_error(client):
    r = client.post("/api/v1/catalogs/counterparties", json={"code": "KA-001", "name": "Дубль"})
    assert r.status_code == 422
    body = r.json()
    assert body["error"] == "validation_failed"
    assert body["details"][0]["field"] == "code"
    assert body["details"][0]["code"] == "duplicate"


def test_missing_reference_is_readable_error(client):
    r = client.post("/api/v1/catalogs/contracts", json={
        "code": "DOG-999", "name": "№ 999", "org_id": ORG,
        "counterparty_id": "11111111-1111-1111-1111-111111111111"})
    assert r.status_code == 422
    assert r.json()["details"][0] == {
        "field": "counterparty_id", "code": "not_found",
        "message": "Выбранный элемент справочника не найден"}


def test_validation_errors_in_russian(client):
    r = client.post("/api/v1/catalogs/counterparties", json={"code": "", "bin": "123"})
    assert r.status_code == 422
    fields = {d["field"]: d["message"] for d in r.json()["details"]}
    assert fields["code"] == "Поле не может быть пустым"
    assert fields["name"] == "Обязательное поле не заполнено"
    assert fields["bin"] == "Неверный формат"


def test_wrong_item_kind_rejected(client):
    r = client.post("/api/v1/catalogs/items", json={
        "code": "NOM-200", "name": "Что-то", "uom_id": UOM_SHT, "kind": "car"})
    assert r.status_code == 422


def test_not_found(client):
    r = client.get("/api/v1/catalogs/items/11111111-1111-1111-1111-111111111111")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_actions_are_written_to_audit_log(client, db):
    r = client.post("/api/v1/catalogs/units", json={"code": "KG", "name": "кг"})
    uid = r.json()["id"]
    client.post(f"/api/v1/catalogs/units/{uid}/deletion-mark", json={"mark": True})
    actions = [row["action"] for row in db.execute(
        "SELECT action FROM audit_log WHERE object_id = %s ORDER BY id", (uid,))]
    assert actions == ["create", "mark"]
