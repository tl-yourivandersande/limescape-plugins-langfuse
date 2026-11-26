import copy
import importlib.util
import json
import os
from pathlib import Path

import pytest
from limescape_plugin_sdk.testing import FakeContext, run_node

NODE_DIR = Path(__file__).resolve().parents[1]
REPO = NODE_DIR.parents[2]

# Synthetic dossier: an invented municipality, invented names and numbers.
ZAAK = {"zaaknummer": "900001-2024", "zaaktype": "Bezwaar", "status": "In behandeling",
        "registratiedatum": "2024-11-06", "startdatum": None}
ITEM = {
    "id": "voorbeeld-set-D01",
    "dataset_id": "dataset-1",
    "metadata": {"dossier": "D01", "split": "ontwikkel"},
    "input": {
        "zaak": {**ZAAK, "besluitdatum": "2024-11-06"},
        "documenten": [
            {"id": "D01-01", "naam": "Primair besluit",
             "tekst": "Datum: 6 november 2024. Het opruimen kost € 187,00 per overtreding. De regels staan in "
                      "artikel 10 Afvalstoffenverordening Voorbeeldstad 2009 en\nartikel 6 Uitvoeringsbesluit "
                      "Afvalstoffen Voorbeeldstad 2018."},
            {"id": "D01-02", "naam": "Bezwaarschrift",
             "tekst": "Voorbeeldstad, 5-11-2024. Ik heb het doosje in de container gedaan. "
                      "Ik verzoek u de boete niet op te leggen."},
        ],
    },
    "expected_output": {
        "zaakoverzicht": ZAAK,
        "documenten": [{"document_id": "D01-01", "naam": "Primair besluit"},
                       {"document_id": "D01-02", "naam": "Bezwaarschrift"}],
        "primair_besluit": {"document_id": "D01-01", "genoemde_grondslagen": [
            "artikel 10 Afvalstoffenverordening Voorbeeldstad 2009",
            "artikel 6 Uitvoeringsbesluit Afvalstoffen Voorbeeldstad 2018"]},
    },
}
GOED = {
    "zaakoverzicht": {**ZAAK, "toelichting": None},
    "documenten": [{"document_id": "D01-01", "naam": "Primair besluit"},
                   {"document_id": "D01-02", "naam": "Bezwaarschrift"}],
    "primair_besluit": {
        "document_id": "D01-01", "besloten": "Opruimkosten doorberekend", "aanleiding": None,
        "genoemde_grondslagen": [
            {"tekst": "artikel 10 Afvalstoffenverordening Voorbeeldstad 2009", "document_id": "D01-01"},
            {"tekst": "artikel 6 Uitvoeringsbesluit Afvalstoffen Voorbeeldstad 2018", "document_id": "D01-01"}],
        "bedragen": [{"tekst": "€ 187,00 per overtreding", "document_id": "D01-01"}],
        "datums": [{"tekst": "6 november 2024", "document_id": "D01-01"}]},
    "bezwaargronden": [{"tekst": "Het doosje zat in de container", "actor": "bezwaarmaker", "document_id": "D01-02"}],
    "feiten": [],
    "partijstandpunten": [{"tekst": "Verzoekt de boete niet op te leggen", "actor": "bezwaarmaker",
                           "document_id": "D01-02"}],
    "tijdlijn": [{"datum": "05-11-2024", "gebeurtenis": "Bezwaarschrift opgesteld", "document_id": "D01-02"}],
}
NON_NEGOTIABLES = {"a_uitvoerformaat", "a_geen_kernoordeel", "a_bron_bestaat", "a_bron_bij_bevinding",
                   "a_grondslagen_letterlijk", "a_waarden_in_bron", "a_geen_externe_bronnen",
                   "a_wetsartikelen_in_dossier"}
# What the eval agent returns, the way the agent call hands it to the flow.
AGENT_CALL = {"analyse_afronden_eval": {
    "dossier_inlezen_eval": {
        "zaakoverzicht": {**ZAAK, "startdatum": "niet beschikbaar", "zaak_url": "https://zaken.example.test/1"},
        "stukken": [{"id": "D01-01", "titel": "Primair besluit"}, {"id": "D01-02", "titel": "Bezwaarschrift"}],
    },
    "documentrollen_bepalen_eval": {"documenten": [
        {"document_id": "D01-01", "rol": "primair besluit", "datum_iso": "2024-11-06"}]},
    "primair_besluit_extraheren_eval": {
        "gevonden": True, "bron_document": "Primair besluit",
        "wat_is_besloten": {"samenvatting": "Opruimkosten doorberekend"},
        "genoemde_grondslagen": [
            {"grondslag": "artikel 10 Afvalstoffenverordening Voorbeeldstad 2009"},
            {"grondslag": "artikel 6 Uitvoeringsbesluit Afvalstoffen Voorbeeldstad 2018"}],
        "bedragen": [{"bedrag": "€ 187,00", "betekenis": "per overtreding"}],
        "datums_en_termijnen": [{"datum_of_termijn": "6 november 2024", "betekenis": "datum besluit"}]},
    "bezwaargronden_extraheren_eval": {
        "bron_document": "D01-02", "bezwaarmaker": {"naam": "bezwaarmaker"},
        "gronden": [{"grond": "Het doosje zat in de container"}]},
    "feiten_en_standpunten_extraheren_eval": {
        "feiten": [], "partijstandpunten": [
            {"standpunt": "Verzoekt de boete niet op te leggen", "actor": "bezwaarmaker", "bron_document": "D01-02"}]},
    "tijdlijn_opstellen_eval": {"gebeurtenissen": [
        {"datum": "2024-11-05", "gebeurtenis": "Bezwaarschrift opgesteld", "bron_document": "D01-02 Bezwaarschrift"}]},
}}


async def check(analysis, analysis_format="contract"):
    result = await run_node(
        NODE_DIR, FakeContext(granted=[]),
        params={"operation": "checks", "item": ITEM, "analysis": analysis, "analysis_format": analysis_format},
        output_key="controles",
    )
    return result[0]["controles"] if "controles" in result[0] else result[0]


def values(result) -> dict:
    return {score["name"]: score["value"] for score in result["scores"]}


async def test_a_correct_analysis_passes_every_check():
    result = await check(GOED)

    scores = values(result)
    assert len(scores) == 18
    assert scores.pop("a_grondslagen_recall") == 1.0
    assert all(value is True for value in scores.values()), scores
    assert NON_NEGOTIABLES <= set(scores)
    assert result["non_negotiables_passed"] is True and result["failed"] == []
    assert (result["dossier"], result["task_error"], result["output"]) == ("D01", "", GOED)
    assert len(result["evaluator_versie"]) == 12
    assert all(score["data_type"] == "BOOLEAN" for score in result["scores"] if score["name"] != "a_grondslagen_recall")


async def test_a_legal_verdict_fails_only_that_hard_requirement():
    """The smoke test of the evals repository: the reference plus one forbidden sentence."""
    output = copy.deepcopy(GOED)
    output["feiten"] = [{"tekst": "Het bezwaar is ongegrond.", "document_id": "D01-01"}]

    result = await check(output)

    assert result["failed"] == ["a_geen_kernoordeel"]
    assert result["non_negotiables_passed"] is False
    comment = next(score["comment"] for score in result["scores"] if score["name"] == "a_geen_kernoordeel")
    assert comment == "oordeel: ongegrond"


async def test_an_invented_source_and_a_missed_ground_are_reported_with_the_reason():
    output = copy.deepcopy(GOED)
    output["tijdlijn"][0]["document_id"] = "D01-07"
    del output["primair_besluit"]["genoemde_grondslagen"][1]

    result = await check(output)

    by_name = {score["name"]: score for score in result["scores"]}
    assert by_name["a_bron_bestaat"]["comment"] == "bestaan niet: tijdlijn[0]: D01-07"
    assert by_name["a_grondslagen_letterlijk"]["value"] is False
    assert by_name["a_grondslagen_recall"]["value"] == 0.5
    assert by_name["a_grondslagen_recall"]["metadata"] == {"gevonden": 1, "verwacht": 2}
    assert result["non_negotiables_passed"] is False


async def test_the_agent_output_is_converted_to_the_output_contract_before_checking():
    """Titles and prefixed references become document ids; what the case system does not know becomes null."""
    result = await check(AGENT_CALL, "agent")

    output = result["output"]
    assert output["primair_besluit"]["document_id"] == "D01-01"
    assert output["tijdlijn"] == [{"datum": "2024-11-05", "gebeurtenis": "Bezwaarschrift opgesteld", "document_id": "D01-02"}]
    assert output["zaakoverzicht"]["startdatum"] is None and "zaak_url" not in output["zaakoverzicht"]
    assert output["documenten"][0] == {"document_id": "D01-01", "naam": "Primair besluit", "datum": "2024-11-06",
                                       "status": None, "type": "primair besluit"}
    assert result["non_negotiables_passed"] is True, result["failed"]


@pytest.mark.parametrize(
    ("analysis", "reason"),
    [
        ({"error": "Node Tijdlijn opstellen eval failed", "status": "failed"},
         "de agent gaf een fout: Node Tijdlijn opstellen eval failed"),
        ({}, "de agent gaf geen analyse terug"),
        ({"status": "completed"}, "de agent gaf geen analyse_afronden_eval terug"),
        ({"analyse_afronden_eval": "geen json"}, "analyse_afronden_eval is geen JSON"),
    ],
)
async def test_a_failed_agent_call_gives_a_task_error_and_no_scores(analysis, reason):
    """The flow records the item as failed in Langfuse instead of scoring an empty analysis."""
    result = await check(analysis, "agent")

    assert result["task_error"] == reason
    assert (result["output"], result["scores"], result["non_negotiables_passed"]) == (None, [], False)


async def test_an_item_without_expected_output_is_refused():
    result = await run_node(NODE_DIR, FakeContext(granted=[]), params={
        "operation": "checks", "item": {"input": {"documenten": []}}, "analysis": GOED, "analysis_format": "contract"})

    assert result == [{"error": "het dataset-item mist input.documenten of expected_output"}]


async def summary(**params):
    return await run_node(NODE_DIR, FakeContext(granted=[]), params={"operation": "run_summary", **params},
                          output_key="samenvatting")


async def loop_results():
    """Three dossiers the way a loop over an agent call collects them: one good, one with a verdict, one failed."""
    good = await check(GOED)
    with_verdict = copy.deepcopy(GOED)
    with_verdict["feiten"] = [{"tekst": "Het bezwaar is ongegrond.", "document_id": "D01-01"}]
    bad = {**await check(with_verdict), "dossier": "D02"}
    failed = {**await check({}, "agent"), "dossier": "D03"}
    return [{"controles": good}, {"controles": bad}, {"controles": failed}]


async def test_run_summary_gives_the_two_run_scores_and_the_regressions():
    results = await loop_results()
    baseline = {"run": "dossier-analyse-v6", "dossiers": {"D01": True, "D02": True, "D03": False, "D04": True, "D09": True}}
    expected_items = {"items": [{"id": f"x-D0{n}", "metadata": {"dossier": f"D0{n}"}} for n in (1, 2, 3, 4)]}

    result = (await summary(results=results, baseline=json.dumps(baseline), expected_items=expected_items))[0]["samenvatting"]

    scores = {score["name"]: score for score in result["scores"]}
    # Like the runner of the evals repository, a dossier whose analysis failed is left out of the totals.
    assert scores["a_non_negotiables_geslaagd"]["value"] == 0.5
    assert scores["a_non_negotiables_geslaagd"]["comment"] == "1/2 dossiers zonder blokkerende fout"
    assert scores["a_grondslagen_recall_totaal"]["value"] == 1.0
    assert scores["a_grondslagen_recall_totaal"]["comment"] == "4/4 genoemde grondslagen overgenomen"
    assert result["per_dossier"] == {"D01": True, "D02": False, "D03": False}
    assert (result["failed_tasks"], result["not_run"]) == (["D03"], ["D04"])
    # D09 is approved but not part of this run, D03 was not approved: neither is a regression.
    assert result["regressions"] == ["D02: haalde de non-negotiables, nu niet", "D04: niet gedraaid"]
    assert result["gate_passed"] is False
    assert result["summary"].startswith("1/3 dossiers halen alle harde eisen; analyse mislukt: D03; niet gedraaid: D04")


async def test_run_summary_of_one_good_dossier_passes_the_gate():
    """A loop that ran once hands over a single result instead of a list."""
    result = (await summary(results={"controles": await check(GOED)},
                            baseline={"run": "v6", "dossiers": {"D01": True, "D02": True}},
                            fail_on_regression="1"))[0]["samenvatting"]

    assert result["gate_passed"] is True and result["regressions"] == []
    assert result["summary"] == "1/1 dossiers halen alle harde eisen; geen terugval t.o.v. baseline v6"


async def test_run_summary_can_stop_the_flow_on_a_regression():
    result = await summary(results=await loop_results(), baseline={"run": "v6", "dossiers": {"D02": True}},
                           fail_on_regression="1")

    assert list(result[0]) == ["error"]
    assert "terugval t.o.v. baseline v6: D02: haalde de non-negotiables, nu niet" in result[0]["error"]


async def test_run_summary_without_results_is_refused():
    assert await summary(results=[{"andere_node": {"x": 1}}]) == [
        {"error": "results bevat geen enkel resultaat van 'Dossier controleren'"}]


@pytest.mark.skipif(not os.environ.get("EVALS_REPO"), reason="set EVALS_REPO to a checkout of the evals repository")
def test_the_checks_are_the_ones_in_the_evals_repository():
    """The generated copy has not drifted from its source (run locally before a release)."""
    spec = importlib.util.spec_from_file_location("sync", REPO / "scripts" / "sync_fase_a_checks.py")
    sync = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(sync)

    for name, content in sync.generate(Path(os.environ["EVALS_REPO"])).items():
        assert (NODE_DIR / name).read_text(encoding="utf-8") == content, f"{name} is out of date; run the sync script"
