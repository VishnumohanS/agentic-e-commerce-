"""MCP, UCP and A2A protocol tests."""

from __future__ import annotations

import pytest

from app.core.exceptions import ProductNotFoundError, ProtocolError
from app.models.protocol import INVALID_PARAMS, METHOD_NOT_FOUND, A2AMessage


class TestMCPRegistry:
    def test_tools_are_advertised_with_schemas(self, tools):
        names = {tool["name"] for tool in tools.definitions()}
        assert {"search_products", "get_product", "check_inventory"} <= names
        assert all("inputSchema" in tool for tool in tools.definitions())

    def test_search_returns_matches(self, tools):
        result = tools.call("search_products", {"query": "bluetooth headphones", "limit": 3})
        assert result["count"] > 0
        assert all("price" in item for item in result["results"])

    def test_search_respects_the_price_ceiling(self, tools):
        result = tools.call("search_products", {"query": "audio", "max_price": 80000})
        assert all(item["price"] <= 80000 for item in result["results"])

    def test_search_excludes_out_of_stock_by_default(self, tools):
        result = tools.call("search_products", {"query": "earbuds", "limit": 10})
        assert "prd_eb_003" not in {item["product_id"] for item in result["results"]}

    def test_search_requires_a_query(self, tools):
        with pytest.raises(ProtocolError):
            tools.call("search_products", {"query": "   "})

    def test_get_product_returns_a_product(self, tools):
        assert tools.call("get_product", {"product_id": "prd_hp_001"})["name"]

    def test_get_unknown_product_raises(self, tools):
        with pytest.raises(ProductNotFoundError):
            tools.call("get_product", {"product_id": "nope"})

    def test_check_inventory_reports_availability(self, tools):
        assert tools.call("check_inventory", {"product_id": "prd_hp_002", "quantity": 1})["in_stock"]

    def test_unknown_tool_raises(self, tools):
        with pytest.raises(ProtocolError):
            tools.call("drop_database", {})

    def test_non_object_arguments_are_rejected(self, tools):
        with pytest.raises(ProtocolError):
            tools.call("get_product", ["prd_hp_001"])  # type: ignore[arg-type]


class TestMCPOverHTTP:
    def test_tools_list(self, merchant_client):
        response = merchant_client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        assert response.status_code == 200
        assert len(response.json()["result"]["tools"]) >= 4

    def test_tools_call(self, merchant_client):
        response = merchant_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "search_products", "arguments": {"query": "headphones"}},
            },
        )
        assert response.json()["result"]["structuredContent"]["count"] > 0

    def test_unknown_method_returns_an_rpc_error(self, merchant_client):
        response = merchant_client.post("/mcp", json={"jsonrpc": "2.0", "id": 3, "method": "nope"})
        assert response.json()["error"]["code"] == METHOD_NOT_FOUND

    def test_missing_tool_name_returns_invalid_params(self, merchant_client):
        response = merchant_client.post(
            "/mcp", json={"jsonrpc": "2.0", "id": 4, "method": "tools/call", "params": {}}
        )
        assert response.json()["error"]["code"] == INVALID_PARAMS

    def test_tool_errors_are_reported_as_rpc_errors(self, merchant_client):
        response = merchant_client.post(
            "/mcp",
            json={
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {"name": "get_product", "arguments": {"product_id": "ghost"}},
            },
        )
        assert response.json()["error"]["data"]["code"] == "product_not_found"


class TestUCPCatalog:
    def test_catalog_is_valid_jsonld(self, merchant_client):
        body = merchant_client.get("/catalog").json()
        assert body["@type"] == "ItemList"
        assert "@context" in body
        assert body["numberOfItems"] == len(body["itemListElement"])

    def test_products_expose_offers_with_price_and_availability(self, merchant_client):
        item = merchant_client.get("/catalog").json()["itemListElement"][0]["item"]
        offer = item["offers"]
        assert offer["priceCurrency"] == "INR"
        assert offer["availability"].startswith("https://schema.org/")
        assert isinstance(offer["acp:priceMinorUnits"], int)

    def test_minor_units_match_the_displayed_price(self, merchant_client):
        for element in merchant_client.get("/catalog").json()["itemListElement"]:
            offer = element["item"]["offers"]
            assert round(float(offer["price"]) * 100) == offer["acp:priceMinorUnits"]

    def test_out_of_stock_products_are_marked(self, merchant_client):
        items = {
            element["item"]["sku"]: element["item"]
            for element in merchant_client.get("/catalog").json()["itemListElement"]
        }
        assert items["prd_eb_003"]["offers"]["availability"].endswith("OutOfStock")

    def test_catalog_advertises_capabilities(self, merchant_client):
        assert "purchase.create_order" in merchant_client.get("/catalog").json()["acp:capabilities"]


class TestA2AProtocol:
    def test_agent_card_lists_skills_and_interfaces(self, merchant_client):
        card = merchant_client.get("/.well-known/agent.json").json()
        assert card["protocolVersion"]
        assert {skill["id"] for skill in card["skills"]} >= {"catalog.search", "purchase.confirm"}

    def test_valid_message_is_handled(self, merchant_client):
        response = merchant_client.post(
            "/a2a/message",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": "m1",
                        "role": "user",
                        "contextId": "txn_1",
                        "parts": [
                            {"kind": "data", "data": {"skill": "catalog.search", "input": {"query": "audio"}}}
                        ],
                    }
                },
            },
        )
        assert response.json()["result"]["contextId"] == "txn_1"

    def test_missing_api_key_is_rejected(self, merchant_app):
        from fastapi.testclient import TestClient

        with TestClient(merchant_app) as client:
            response = client.post("/a2a/message", json={"jsonrpc": "2.0", "id": "1", "method": "message/send"})
        assert response.status_code == 401

    def test_wrong_api_key_is_rejected(self, merchant_client):
        response = merchant_client.post(
            "/a2a/message",
            headers={"X-A2A-Key": "wrong"},
            json={"jsonrpc": "2.0", "id": "1", "method": "message/send"},
        )
        assert response.status_code == 401

    def test_wrong_jsonrpc_version_is_rejected(self, merchant_client):
        response = merchant_client.post(
            "/a2a/message", json={"jsonrpc": "1.0", "id": "1", "method": "message/send"}
        )
        assert response.json()["error"]["code"] != 0

    def test_unknown_method_is_rejected(self, merchant_client):
        response = merchant_client.post(
            "/a2a/message", json={"jsonrpc": "2.0", "id": "1", "method": "message/delete"}
        )
        assert response.json()["error"]["code"] == METHOD_NOT_FOUND

    def test_malformed_message_is_rejected(self, merchant_client):
        response = merchant_client.post(
            "/a2a/message",
            json={"jsonrpc": "2.0", "id": "1", "method": "message/send", "params": {"message": {}}},
        )
        assert response.json()["error"]["code"] == INVALID_PARAMS

    def test_message_without_parts_is_rejected(self):
        with pytest.raises(Exception):
            A2AMessage.model_validate({"messageId": "m", "role": "user", "parts": []})

    def test_unknown_skill_is_rejected(self, merchant_client):
        response = merchant_client.post(
            "/a2a/message",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": "m1",
                        "role": "user",
                        "contextId": "txn_1",
                        "parts": [{"kind": "data", "data": {"skill": "refund.everything", "input": {}}}],
                    }
                },
            },
        )
        assert response.json()["error"]["data"]["code"] == "protocol_error"

    def test_transaction_id_is_propagated_into_the_ledger(self, merchant_client, merchant_container):
        merchant_client.post(
            "/a2a/message",
            json={
                "jsonrpc": "2.0",
                "id": "1",
                "method": "message/send",
                "params": {
                    "message": {
                        "messageId": "m1",
                        "role": "user",
                        "contextId": "txn_trace",
                        "parts": [{"kind": "data", "data": {"skill": "catalog.search", "input": {"query": "audio"}}}],
                    }
                },
            },
        )
        assert merchant_container.ledger.read_transaction("txn_trace")


class TestA2AClient:
    def test_client_reads_the_agent_card(self, a2a_client):
        assert a2a_client.fetch_agent_card()["skills"]

    def test_client_searches_the_catalog(self, a2a_client):
        result = a2a_client.search_catalog("headphones", transaction_id="txn_1")
        assert result["count"] > 0

    def test_remote_errors_become_typed_exceptions(self, a2a_client):
        with pytest.raises(ProductNotFoundError):
            a2a_client.send("catalog.get_product", {"product_id": "ghost"}, transaction_id="txn_1")

    def test_unreachable_merchant_raises(self, settings):
        from buyer_agent.services.a2a_client import A2AClient
        from app.core.exceptions import AgentUnavailableError

        client = A2AClient("http://127.0.0.1:9", api_key="x", timeout=0.3, max_retries=1)
        with pytest.raises(AgentUnavailableError):
            client.search_catalog("x", transaction_id="txn_1")

    def test_bad_credentials_raise(self, transport):
        from buyer_agent.services.a2a_client import A2AClient
        from app.core.exceptions import AgentUnavailableError

        client = A2AClient("http://merchant.test", api_key="wrong", transport=transport)
        with pytest.raises(AgentUnavailableError):
            client.search_catalog("x", transaction_id="txn_1")
