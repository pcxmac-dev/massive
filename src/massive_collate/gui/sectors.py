"""Sector and industry for the market map, from a company's SIC code.

The reference data carries the SEC's SIC industry code, not a market sector scheme. SIC ranges map
onto the eleven sectors market maps use (Technology, Financial, Healthcare, ...), and the SIC
description, tidied, names the industry. A few SIC codes lump unlike companies together (7370
"computer programming" holds Alphabet and Meta; 7389 "business services" holds Visa and
Accenture), so the largest such companies are placed by hand in OVERRIDES.
"""
from __future__ import annotations

import re

SECTORS = ("Technology", "Communication Services", "Consumer Cyclical", "Consumer Defensive", "Financial",
           "Healthcare", "Industrials", "Energy", "Basic Materials", "Real Estate", "Utilities")

# (first SIC, last SIC, sector), most specific first: the first range containing the code wins.
RANGES: tuple[tuple[int, int, str], ...] = (
    (2830, 2836, "Healthcare"), (2840, 2844, "Consumer Defensive"), (3630, 3639, "Consumer Cyclical"),
    (3650, 3699, "Technology"), (3570, 3579, "Technology"), (3711, 3716, "Consumer Cyclical"),
    (3714, 3714, "Consumer Cyclical"), (3812, 3812, "Industrials"), (3820, 3829, "Technology"),
    (3840, 3851, "Healthcare"), (3861, 3861, "Technology"), (3873, 3873, "Consumer Cyclical"),
    (4700, 4729, "Consumer Cyclical"), (4950, 4959, "Industrials"), (5122, 5122, "Healthcare"),
    (5140, 5149, "Consumer Defensive"), (5331, 5331, "Consumer Defensive"), (5400, 5499, "Consumer Defensive"),
    (5912, 5912, "Healthcare"), (6324, 6324, "Healthcare"), (6500, 6553, "Real Estate"), (6798, 6798, "Real Estate"),
    (7310, 7319, "Communication Services"), (7320, 7329, "Financial"), (7370, 7379, "Technology"), (7800, 7849, "Communication Services"),
    (7990, 7999, "Communication Services"), (8000, 8099, "Healthcare"), (8730, 8734, "Healthcare"),
    (100, 999, "Consumer Defensive"), (1000, 1099, "Basic Materials"), (1200, 1399, "Energy"),
    (1400, 1499, "Basic Materials"), (1500, 1799, "Industrials"), (2000, 2199, "Consumer Defensive"),
    (2200, 2399, "Consumer Cyclical"), (2400, 2499, "Basic Materials"), (2500, 2599, "Consumer Cyclical"),
    (2600, 2699, "Basic Materials"), (2700, 2799, "Communication Services"), (2800, 2899, "Basic Materials"),
    (2900, 2999, "Energy"), (3000, 3099, "Basic Materials"), (3100, 3199, "Consumer Cyclical"),
    (3200, 3399, "Basic Materials"), (3400, 3569, "Industrials"), (3580, 3629, "Industrials"),
    (3640, 3649, "Industrials"), (3700, 3799, "Industrials"), (3800, 3899, "Industrials"),
    (3900, 3999, "Consumer Cyclical"), (4000, 4799, "Industrials"), (4800, 4899, "Communication Services"),
    (4900, 4999, "Utilities"), (5000, 5199, "Industrials"), (5200, 5999, "Consumer Cyclical"),
    (6000, 6799, "Financial"), (7000, 7299, "Consumer Cyclical"), (7300, 7399, "Industrials"),
    (7500, 7699, "Consumer Cyclical"), (7700, 7999, "Consumer Cyclical"), (8000, 8999, "Industrials"),
    (9000, 9999, "Industrials"),
)

# Companies whose SIC code places them with unlike businesses: (sector, industry).
OVERRIDES: dict[str, tuple[str, str]] = {
    "GOOG": ("Communication Services", "Internet Content & Information"),
    "GOOGL": ("Communication Services", "Internet Content & Information"),
    "META": ("Communication Services", "Internet Content & Information"),
    "V": ("Financial", "Credit Services"), "MA": ("Financial", "Credit Services"),
    "PYPL": ("Financial", "Credit Services"),
    "ACN": ("Technology", "Information Technology Services"),
    "UBER": ("Technology", "Software - Application"),
    "LRCX": ("Technology", "Semiconductor Equipment"), "KLAC": ("Technology", "Semiconductor Equipment"),
    "DHR": ("Healthcare", "Diagnostics & Research"), "TMO": ("Healthcare", "Diagnostics & Research"),
    "SPCX": ("Industrials", "Aerospace & Defense"),
    "PANW": ("Technology", "Software - Infrastructure"),
}

# Tidier names for common SIC descriptions.
INDUSTRY_NAMES: dict[str, str] = {
    "SEMICONDUCTORS & RELATED DEVICES": "Semiconductors",
    "SERVICES-PREPACKAGED SOFTWARE": "Software",
    "PHARMACEUTICAL PREPARATIONS": "Drug Manufacturers",
    "BIOLOGICAL PRODUCTS, (NO DIAGNOSTIC SUBSTANCES)": "Biotechnology",
    "NATIONAL COMMERCIAL BANKS": "Banks", "STATE COMMERCIAL BANKS": "Banks - Regional",
    "SECURITY BROKERS, DEALERS & FLOTATION COMPANIES": "Capital Markets",
    "FIRE, MARINE & CASUALTY INSURANCE": "Insurance", "HOSPITAL & MEDICAL SERVICE PLANS": "Healthcare Plans",
    "ELECTRONIC COMPUTERS": "Consumer Electronics", "COMPUTER STORAGE DEVICES": "Computer Hardware",
    "COMPUTER COMMUNICATIONS EQUIPMENT": "Communication Equipment",
    "RADIO & TV BROADCASTING & COMMUNICATIONS EQUIPMENT": "Communication Equipment",
    "PETROLEUM REFINING": "Oil & Gas", "REAL ESTATE INVESTMENT TRUSTS": "REITs",
    "TELEPHONE COMMUNICATIONS (NO RADIOTELEPHONE)": "Telecom Services", "ELECTRIC SERVICES": "Utilities - Electric",
    "RETAIL-VARIETY STORES": "Discount Stores", "RETAIL-CATALOG & MAIL-ORDER HOUSES": "Internet Retail",
    "RETAIL-EATING PLACES": "Restaurants", "RETAIL-LUMBER & OTHER BUILDING MATERIALS DEALERS": "Home Improvement Retail",
    "RETAIL-DRUG STORES AND PROPRIETARY STORES": "Pharmacies & Health Plans",
    "MOTOR VEHICLES & PASSENGER CAR BODIES": "Auto Manufacturers", "AIRCRAFT": "Aerospace & Defense",
    "AIRCRAFT ENGINES & ENGINE PARTS": "Aerospace & Defense", "GUIDED MISSILES & SPACE VEHICLES & PARTS": "Aerospace & Defense",
    "SERVICES-VIDEO TAPE RENTAL": "Entertainment", "SERVICES-MISCELLANEOUS AMUSEMENT & RECREATION": "Entertainment",
    "TRANSPORTATION SERVICES": "Travel Services", "SOAP, DETERGENTS, CLEANG PREPARATIONS, PERFUMES, COSMETICS": "Household & Personal Products",
    "CIGARETTES": "Tobacco", "BEVERAGES": "Beverages",
    "SERVICES-CONSUMER CREDIT REPORTING, COLLECTION AGENCIES": "Financial Data & Exchanges",
    "RETAIL-FAMILY CLOTHING STORES": "Apparel Retail", "ORTHOPEDIC, PROSTHETIC & SURGICAL APPLIANCES & SUPPLIES": "Medical Devices",
    "ELECTRONIC & OTHER ELECTRICAL EQUIPMENT (NO COMPUTER EQUIP)": "Electrical Equipment",
    "MISC INDUSTRIAL & COMMERCIAL MACHINERY & EQUIPMENT": "Industrial Machinery", "RAILROADS, LINE-HAUL OPERATING": "Railroads",
    "SERVICES-COMPUTER PROCESSING & DATA PREPARATION": "Information Technology Services",
    "SERVICES-COMPUTER PROGRAMMING, DATA PROCESSING, ETC.": "Information Technology Services",
    "INDUSTRIAL INORGANIC CHEMICALS": "Specialty Chemicals", "FINANCE SERVICES": "Credit Services",
    "CONSTRUCTION MACHINERY & EQUIP": "Construction Machinery", "FARM MACHINERY & EQUIPMENT": "Farm Machinery",
    "COMPUTER & OFFICE EQUIPMENT": "Computer Hardware", "ELECTRONIC CONNECTORS": "Electronic Components",
    "SPECIAL INDUSTRY MACHINERY, NEC": "Industrial Machinery", "SERVICES-BUSINESS SERVICES, NEC": "Business Services",
}


def industry_name(description: str | None) -> str:
    if not description:
        return "Other"
    d = description.strip().upper()
    if d in INDUSTRY_NAMES:
        return INDUSTRY_NAMES[d]
    d = re.sub(r"^(SERVICES|RETAIL|WHOLESALE)-", "", d)
    d = re.sub(r"\s*\(.*?\)", "", d).replace(", NEC", "").replace(" NEC", "")
    words = [w.capitalize() if w not in ("&", "OF", "AND") else w.lower() for w in d.split()]
    return " ".join(words).replace(" ,", ",") or "Other"


def classify(ticker: str, sic_code, sic_description: str | None) -> tuple[str, str]:
    """(sector, industry) for a company; ('Other', ...) without a SIC code."""
    if ticker.upper() in OVERRIDES:
        return OVERRIDES[ticker.upper()]
    try:
        code = int(sic_code)
    except (TypeError, ValueError):
        return "Other", industry_name(sic_description)
    sector = next((s for lo, hi, s in RANGES if lo <= code <= hi), "Other")
    return sector, industry_name(sic_description)
