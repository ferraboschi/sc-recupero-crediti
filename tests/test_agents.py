"""Agente di vendita dai tag cliente Shopify (backend/engine/agents.py).

Regole owner 9/11/2026: Maggioni / Izzo / Scotti; Rendina → Izzo;
Sgariglia De Leo → Maggioni; combinazioni mostrate per intero.
"""
import pytest

from backend.engine.agents import AGENTS, agents_from_tags, agents_of, split_tags


class TestSplitTags:
    def test_none_and_empty(self):
        assert split_tags(None) == []
        assert split_tags("") == []
        assert split_tags("  ,  , ") == []

    def test_shopify_string(self):
        assert split_tags("B2B, Maggioni") == ["B2B", "Maggioni"]
        assert split_tags("B2B,Izzo ,  scp") == ["B2B", "Izzo", "scp"]

    def test_list_input(self):
        assert split_tags(["B2B", " SCOTTI ", None, ""]) == ["B2B", "SCOTTI"]


class TestAgentsFromTags:
    @pytest.mark.parametrize("tags,expected", [
        (None, []),
        ("", []),
        ("B2B", []),
        ("B2B, scp, Wholesale", []),
        ("B2B, Maggioni", ["Maggioni"]),
        ("B2B, Izzo", ["Izzo"]),
        ("B2B, Izzo, scp", ["Izzo"]),
        ("B2B, SCOTTI", ["Scotti"]),  # grafia reale su Shopify
        ("scotti", ["Scotti"]),
        ("maggioni", ["Maggioni"]),
    ])
    def test_single_agent_any_case(self, tags, expected):
        assert agents_from_tags(tags) == expected

    def test_rendina_is_shown_as_izzo(self):
        assert agents_from_tags("B2B, rendina") == ["Izzo"]
        assert agents_from_tags("B2B, Rendina, Izzo") == ["Izzo"]  # niente doppioni

    def test_sgariglia_de_leo_is_shown_as_maggioni(self):
        assert agents_from_tags("B2B, sgariglia de leo") == ["Maggioni"]
        assert agents_from_tags("B2B, Sgariglia De Leo, Maggioni") == ["Maggioni"]

    def test_combinations_keep_every_agent_in_fixed_order(self):
        assert agents_from_tags("Scotti, Maggioni") == ["Maggioni", "Scotti"]
        assert agents_from_tags("Izzo, B2B, Maggioni") == ["Maggioni", "Izzo"]
        assert agents_from_tags("scotti, rendina, sgariglia de leo") == list(AGENTS)

    def test_word_boundary_prevents_false_positives(self):
        assert agents_from_tags("Pizzoccheri") == []
        assert agents_from_tags("Maggionida") == []
        assert agents_from_tags("izzo-roma") == ["Izzo"]  # trattino = confine di parola

    def test_whitespace_and_separators_tolerated(self):
        assert agents_from_tags("  B2B ,  MAGGIONI  ") == ["Maggioni"]
        assert agents_from_tags(["B2B", "Izzo"]) == ["Izzo"]

    def test_returns_new_list_each_call(self):
        a = agents_from_tags("Maggioni")
        a.append("x")
        assert agents_from_tags("Maggioni") == ["Maggioni"]


class TestAgentsOf:
    def test_none_customer(self):
        assert agents_of(None) == []

    def test_customer_like_object(self):
        class C:
            tags = "B2B, Scotti"
        assert agents_of(C()) == ["Scotti"]

    def test_object_without_tags_attribute(self):
        assert agents_of(object()) == []
