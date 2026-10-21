from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_m1_m2_evidence_recorded() -> None:
    plan = (ROOT / "docs/plans/USR-279-greenfield-pilot.md").read_text(encoding="utf-8")
    m1 = (ROOT / "docs/evidence/USR-279-M1.md").read_text(encoding="utf-8")
    m2 = (ROOT / "docs/evidence/USR-279-M2.md").read_text(encoding="utf-8")

    assert "https://github.com/GCamposGit/pilot-hello" in plan
    assert "23960d95add7595952c8b8ba679af13becc43348" in plan
    assert "M1 (bootstrap) e M2 (ambiente) concluidos" in plan
    assert "setup/validate/build/smoke" in plan
    assert "https://github.com/GCamposGit/pilot-hello" in m1
    assert "23960d95add7595952c8b8ba679af13becc43348" in m1
    assert "23960d95add7595952c8b8ba679af13becc43348" in m2
    assert "pilot-hello" in m2
    assert "Dokploy" in m2
    assert "separa" in m2.lower()
    assert "g2TBxsWnMna-qn6muYkH2" in plan
    assert "g2TBxsWnMna-qn6muYkH2" in m2


def test_no_resource_recreation() -> None:
    plan = (ROOT / "docs/plans/USR-279-greenfield-pilot.md").read_text(encoding="utf-8")

    assert "M1/M2: verificar e reutilizar as evidencias existentes" in plan
    assert "nao recriar" in plan.lower()
    assert "OA-043" in plan
    assert "nao repetir a criacao" in plan.lower()
