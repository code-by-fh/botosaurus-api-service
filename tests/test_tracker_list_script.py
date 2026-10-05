from scripts.update_tracker_domains import (
    domain_wide_rules,
    exempted_domains,
    is_covered,
    select_domains,
)

SERVER_SECTION = "! *** easylist:easyprivacy/easyprivacy_trackingservers_thirdparty.txt ***"
SITE_SECTION = "! *** easylist:easyprivacy/easyprivacy_specific.txt ***"


def test_domain_rules_are_taken_only_from_server_sections():
    lines = [SITE_SECTION, "||soundcloud.com^$ping", SERVER_SECTION, "||tracker.example^"]

    assert domain_wide_rules(lines) == {"tracker.example"}


def test_third_party_and_request_type_options_keep_a_rule_domain_wide():
    lines = [SERVER_SECTION, "||a.example^$third-party", "||b.example^$script,xmlhttprequest"]

    assert domain_wide_rules(lines) == {"a.example", "b.example"}


def test_site_scoped_path_and_popup_rules_are_ignored():
    lines = [
        SERVER_SECTION,
        "||a.example^$domain=news.example",
        "||b.example/pixel.gif",
        "||c.example^$popup",
    ]

    assert domain_wide_rules(lines) == set()


def test_whole_domain_exemptions_are_collected_but_path_exemptions_are_not():
    lines = ["@@||a.example^$domain=shop.example", "@@||b.example/gtm.js$domain=x.example"]

    assert exempted_domains(lines) == {"a.example"}


def test_is_covered_matches_the_domain_and_its_parents_only():
    assert is_covered("ad.tracker.example", {"tracker.example"})
    assert is_covered("tracker.example", {"tracker.example"})
    assert not is_covered("eviltracker.example", {"tracker.example"})
    assert not is_covered("tracker.example", {"ad.tracker.example"})


def test_selection_drops_exempted_domains_and_covered_subdomains():
    candidates = {"a.example", "ad.a.example", "b.example"}
    ranking = {"a.example": 1, "b.example": 2}

    assert select_domains(candidates, {"b.example"}, ranking) == ["a.example"]


def test_host_takes_the_rank_of_its_parent_domain_but_not_of_shared_hosting():
    candidates = {"g.doubleclick.net", "ads.s3.amazonaws.com", "unranked.example"}
    ranking = {"doubleclick.net": 38, "amazonaws.com": 10}

    assert select_domains(candidates, set(), ranking) == ["g.doubleclick.net"]
