import main


def test_extract_lead_with_addon_fields():
    lead = main.extract_lead({
        "transaction_id": "tx-1", "status": "paid", "payment_link_id": "link-bonus",
        "bump_ids": ["bump-1", "bump-2"], "bump_amount_cents": 1990,
    })
    assert lead["payment_link_id"] == "link-bonus"
    assert lead["bump_ids"] == ["bump-1", "bump-2"]
    assert lead["bump_amount_cents"] == 1990


def test_extract_lead_defaults_and_validation():
    old = main.extract_lead({"transaction_id": "tx-old", "status": "paid"})
    invalid = main.extract_lead({"transaction_id": "tx-bad", "status": "paid", "bump_ids": ["valid", 123], "bump_amount_cents": "1990"})
    raw = main.extract_lead({"event": "PAYMENT_RECEIVED", "payment": {"id": "asaas-1", "value": 29.0, "customer": {}}})
    for lead in (old, raw):
        assert lead["payment_link_id"] == ""
        assert lead["bump_ids"] == []
        assert lead["bump_amount_cents"] == 0
    assert invalid["bump_ids"] == []
    assert invalid["bump_amount_cents"] == 0


def test_addon_flags(monkeypatch):
    monkeypatch.setattr(main, "BONUS_PAYMENT_LINK_IDS", {"bonus-link"})
    assert main.addon_flags("bonus-link", ["bump-1"]) == (True, True)
    assert main.addon_flags("other-link", []) == (False, False)


def test_render_confirmation_email_addon_blocks(monkeypatch):
    monkeypatch.setattr(main, "BONUS_AULA_URL", "https://example.com/aula")
    monkeypatch.setattr(main, "BUMP_KIT_URL", "https://example.com/kit")
    both = main.render_confirmation_email("<script>", True, True)
    neither = main.render_confirmation_email("Ana", False, False)
    assert "Seu bônus do diagnóstico" in both and "Assistir à aula extra" in both
    assert "Seu material adicional" in both and "Baixar o Kit" in both
    assert "https://example.com/aula" in both and "https://example.com/kit" in both
    assert "<script>" not in both
    assert "Seu bônus do diagnóstico" not in neither
    assert "Seu material adicional" not in neither


def test_render_omits_block_when_url_is_empty(monkeypatch):
    monkeypatch.setattr(main, "BONUS_AULA_URL", "")
    monkeypatch.setattr(main, "BUMP_KIT_URL", "")
    rendered = main.render_confirmation_email("Ana", True, True)
    assert "Seu bônus do diagnóstico" not in rendered
    assert "Seu material adicional" not in rendered
