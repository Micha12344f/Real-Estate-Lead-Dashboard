from fastapi.testclient import TestClient

import app.main as main
from app.main import app
from app.services.booking.properties import normalise_property


client = TestClient(app)


def test_available_properties_only_include_status_ids_zero_and_one(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)

    class Properties:
        async def list_properties(self, status_id=None):
            return [
                {"id": 10, "statusId": 0, "street": "Zero Street"},
                {"id": 11, "statusId": 1, "street": "One Street"},
                {"id": 12, "statusId": "0", "street": "String Zero"},
                {"id": 13, "statusId": "1", "street": "String One"},
                {"id": 14, "statusId": 2},
                {"id": 15, "statusId": "3"},
                {"id": 16, "statusId": None},
                {"id": 17},
                {"id": 18, "statusId": 0, "address": {"propertyName": "Live House", "street": "2 Live Road",
                                                       "town": "Reading", "postcode": "RG1 2BB"}},
                {"id": 19, "statusId": 2, "address": {"propertyName": "Gone House", "street": "3 Gone Road",
                                                       "town": "Reading", "postcode": "RG1 3CC"}},
            ]

    monkeypatch.setattr("app.routes.booking.properties.acquaint", Properties())
    response = client.get("/webhook/thames-valley-estates/booking-available-properties")

    assert response.status_code == 200
    properties = response.json()["properties"]
    assert [property["id"] for property in properties] == [10, 11, 12, 13, 18]
    assert properties[-1]["propertyName"] == "Live House"
    assert properties[-1]["address"] == "2 Live Road, Reading, RG1 2BB"


def test_available_properties_keep_an_explicit_status_zero(monkeypatch):
    monkeypatch.setattr(main, "AUTH_DISABLED", True)
    received = []

    class Properties:
        async def list_properties(self, status_id=None):
            received.append(status_id)
            return [{"id": 10, "statusId": 0, "street": "Zero Street"}]

    monkeypatch.setattr("app.routes.booking.properties.acquaint", Properties())
    response = client.get("/webhook/thames-valley-estates/booking-available-properties?statusId=0")

    assert response.status_code == 200
    assert received == [0]
    assert [property["id"] for property in response.json()["properties"]] == [10]


def test_normalise_property_reads_live_nested_address_shape():
    normalised = normalise_property({
        "id": 242, "statusId": 0, "price": 1200, "pricePrefix": 0, "bedrooms": 2,
        "address": {"propertyName": "Riverside Flat", "street": "77 Riverbank Walk",
                    "town": "Reading", "postcode": "RG1 8AB"},
    })
    assert normalised["propertyName"] == "Riverside Flat"
    assert normalised["street"] == "77 Riverbank Walk"
    assert normalised["town"] == "Reading"
    assert normalised["postcode"] == "RG1 8AB"
    assert normalised["address"] == "77 Riverbank Walk, Reading, RG1 8AB"
