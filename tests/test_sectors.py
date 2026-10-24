"""Market map: sectors from SIC codes, overrides, industry names, share-class sizing, day changes."""
from datetime import date

from massive_collate.gui.sectors import SECTORS, classify, industry_name
from massive_collate.gui.server import _sector


def test_sic_ranges_place_companies_in_market_sectors():
    assert classify("NVDA", "3674", "SEMICONDUCTORS & RELATED DEVICES") == ("Technology", "Semiconductors")
    assert classify("WMT", "5331", "RETAIL-VARIETY STORES") == ("Consumer Defensive", "Discount Stores")
    assert classify("AMZN", "5961", "RETAIL-CATALOG & MAIL-ORDER HOUSES") == ("Consumer Cyclical", "Internet Retail")
    assert classify("UNH", "6324", "HOSPITAL & MEDICAL SERVICE PLANS") == ("Healthcare", "Healthcare Plans")
    assert classify("SPGI", "7320", "SERVICES-CONSUMER CREDIT REPORTING, COLLECTION AGENCIES")[0] == "Financial"
    assert classify("PLD", "6798", "REAL ESTATE INVESTMENT TRUSTS") == ("Real Estate", "REITs")
    assert classify("XOM", "2911", "PETROLEUM REFINING")[0] == "Energy"
    assert classify("NEE", "4911", "ELECTRIC SERVICES")[0] == "Utilities"
    assert classify("X", None, None) == ("Other", "Other")
    every = {classify("T", str(code), "x")[0] for code in range(100, 10000, 7)}
    assert every <= set(SECTORS) | {"Other"}


def test_companies_sic_lumps_together_are_placed_by_hand():
    assert classify("GOOGL", "7370", "SERVICES-COMPUTER PROGRAMMING, DATA PROCESSING, ETC.")[0] == "Communication Services"
    assert classify("V", "7389", "SERVICES-BUSINESS SERVICES, NEC") == ("Financial", "Credit Services")
    assert classify("ACN", "7389", "SERVICES-BUSINESS SERVICES, NEC")[0] == "Technology"
    assert classify("LRCX", "3559", "SPECIAL INDUSTRY MACHINERY, NEC") == ("Technology", "Semiconductor Equipment")


def test_industry_names_are_tidied():
    assert industry_name("SERVICES-MANAGEMENT CONSULTING SERVICES") == "Management Consulting Services"
    assert industry_name("RETAIL-AUTO DEALERS & GASOLINE STATIONS") == "Auto Dealers & Gasoline Stations"
    assert industry_name("MEASURING & CONTROLLING DEVICES, NEC") == "Measuring & Controlling Devices"
    assert industry_name("GOLD AND SILVER ORES") == "Gold and Silver Ores"


def test_tiles_are_sized_by_their_own_share_class():
    profile = {"sic_code": "7370", "sic_description": "x", "market_cap": 4.0e12, "class_shares": 5.0e9}
    out = _sector("GOOG", profile, "stock", close=300.0)
    assert out["marketCap"] == 1.5e12 and out["companyCap"] == 4.0e12  # not the whole company twice
    assert _sector("GOOG", {**profile, "class_shares": None}, "stock", 300.0)["marketCap"] == 4.0e12
    assert _sector("BTCUSD", profile, "crypto")["sector"] is None


def test_profiles_and_day_changes_from_the_store(store):
    store.set_details("massive", "AAPL", {"sic_code": "3571", "sic_description": "ELECTRONIC COMPUTERS",
                                          "market_cap": 5e12, "share_class_shares_outstanding": 1.5e10})
    store.set_details("massive", "C:EURUSD", {"name": "Euro"})  # no SIC, no cap: not a company
    assert store.company_profiles() == {"AAPL": {"sic_code": "3571", "sic_description": "ELECTRONIC COMPUTERS",
                                                 "market_cap": 5e12, "class_shares": 1.5e10}}
    store.save_market_snapshot("massive", date(2026, 10, 20), [{"T": "AAPL", "c": 200.0, "v": 1}])
    store.save_market_snapshot("massive", date(2026, 10, 21), [{"T": "AAPL", "c": 210.0, "v": 1}])
    assert round(store.snapshot_changes()["AAPL"], 6) == 5.0


def test_sp500_members_take_their_gics_sector():
    profile = {"sic_code": "3571", "sic_description": "ELECTRONIC COMPUTERS", "market_cap": 3.0e12}
    same = _sector("AAPL", profile, "stock", member={"sector": "Technology", "industry": "Technology Hardware"})
    assert same["sector"] == "Technology" and same["industry"] == "Consumer Electronics"  # the map's own name kept
    assert same["sectorBasis"] == "GICS (S&P 500 list)"
    bank = {"sic_code": "7374", "sic_description": "SERVICES-COMPUTER PROCESSING & DATA PREPARATION"}
    moved = _sector("FIS", bank, "stock", member={"sector": "Financial", "industry": "Transaction & Payment Processing"})
    assert (moved["sector"], moved["industry"]) == ("Financial", "Transaction & Payment Processing")  # GICS both
    assert _sector("NEW", None, "stock", member={"sector": "Energy", "industry": "Oil & Gas Refining"})["sector"] == "Energy"
    assert _sector("X", profile, "stock", member={"sector": "Unknown"})["sectorBasis"] == "SIC"
