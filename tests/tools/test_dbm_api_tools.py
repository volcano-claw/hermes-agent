import json

from tools.dbm_api_tools import (
    _handle_dbm_prospects_insert,
    _normalize_prospect_geo,
    parse_city_postal_department_from_url,
    region_code_for_department,
)


def test_parse_city_uses_url_segment_before_postal_code_not_cabinet_slug():
    parsed = parse_city_postal_department_from_url(
        "https://annuaire.experts-comptables.org/expert-comptable/nice-06000/cabinet-xyz"
    )

    assert parsed == {
        "city": "Nice",
        "postal_code": "06000",
        "department_code": "06",
    }


def test_parse_city_does_not_pollute_from_firm_name_slug():
    prospect = _normalize_prospect_geo(
        {
            "name": "Conseil Societe D Expertise Comptable Et De Commissariat Aux Comptes Nice",
            "city": "Conseil Societe D Expertise Comptable Et De Commissariat Aux Comptes Nice",
            "department_code": "06",
            "region_code": "PACA",
            "source_url": "https://annuaire.experts-comptables.org/expert-comptable/conseil-societe-nice-06000-bd-victor-hugo",
        }
    )

    assert prospect["city"] == "Nice"
    assert prospect["postal_code"] == "06000"
    assert prospect["department_code"] == "06"
    assert prospect["region_code"] == "93"


def test_parse_multiword_city_suffixes_before_postal_code():
    cases = {
        "https://annuaire.experts-comptables.org/expert-comptable/conseil-societe-nice-06000-bd-victor-hugo": "Nice",
        "https://annuaire.experts-comptables.org/expert-comptable/yves-bailleux-cagnes-sur-mer-06800-route": "Cagnes Sur Mer",
        "https://annuaire.experts-comptables.org/expert-comptable/joy-compta-saint-laurent-du-var-06700-centre": "Saint Laurent Du Var",
        "https://annuaire.experts-comptables.org/expert-comptable/cabinet-marseille-13001-centre": "Marseille",
        "https://annuaire.experts-comptables.org/expert-comptable/cabinet-toulon-83000-centre": "Toulon",
        "https://annuaire.experts-comptables.org/expert-comptable/cabinet-avignon-84000-centre": "Avignon",
        "https://annuaire.experts-comptables.org/expert-comptable/34226-insider-cassis-13260": "Cassis",
    }
    for url, expected_city in cases.items():
        assert parse_city_postal_department_from_url(url)["city"] == expected_city


def test_insert_prefers_intel_city_over_polluted_url_city():
    result = json.loads(
        _handle_dbm_prospects_insert(
            {
                "dry_run": True,
                "prospect": {
                    "tier": 2,
                    "category": "expert_comptable_cassis_dryrun_day2",
                    "name": "INSIDER",
                    "city": "34226 Insider Cassis",
                    "department_code": "13",
                    "region_code": "93",
                    "source_url": "https://annuaire.experts-comptables.org/expert-comptable/34226-insider-cassis-13260",
                    "intel": {"city": "Cassis", "postal_code": "13260"},
                },
            }
        )
    )

    body = result["body"]
    assert body["city"] == "Cassis"
    assert body["city"] == body["intel"]["city"]


def test_department_to_region_insee_mapping_paca_and_idf():
    assert region_code_for_department("13") == "93"
    assert region_code_for_department("83") == "93"
    assert region_code_for_department("84") == "93"
    assert region_code_for_department("75") == "11"
    assert region_code_for_department("93") == "11"


def test_insert_dry_run_outputs_normalized_city_and_insee_region_code():
    result = json.loads(
        _handle_dbm_prospects_insert(
            {
                "dry_run": True,
                "prospect": {
                    "tier": 2,
                    "category": "accountant",
                    "name": "Cabinet Xyz Nice",
                    "city": "Cabinet Xyz Nice",
                    "region_code": "PACA",
                    "source_url": "https://annuaire.experts-comptables.org/expert-comptable/nice-06000/cabinet-xyz",
                },
            }
        )
    )

    body = result["body"]
    assert body["city"] == "Nice"
    assert body["postal_code"] == "06000"
    assert body["department_code"] == "06"
    assert body["region_code"] == "93"
    assert body["region_code"] != "PACA"


def test_non_insee_region_string_removed_when_department_unknown():
    prospect = _normalize_prospect_geo({"region_code": "IDF", "name": "No dep"})

    assert "region_code" not in prospect
