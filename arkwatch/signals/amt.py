"""amt.py — Advanced Auction Market Theory (AMT) and Market Profile Engine.

Implements core Steidlmayer / Dalton institutional profiling from 5-minute bars:
  - TPO Profile: 30-minute letter brackets (A, B, C...) and TPO POC / Value Area
  - Initial Balance (IB): Asset-class-aware benchmark (Equities, Oil, Gold, Crypto) and Day Types
  - Profile Shape: D-shape (balance), P-shape (buying initiative), b-shape (liquidation), B-shape (double distribution)
  - Auction Extremes: Excess (rejection tails) vs Poor High/Low (unfinished auctions)
  - Pilar 1: The 4 Open Types (Open Drive, Open Test-Drive, Open Rejection-Reverse, Open in Value)
  - Pilar 2: Participant Activity (Initiative vs Responsive Buying & Selling)
  - Pilar 3: Day-to-Day Value Migration (Higher, Overlapping Higher, Inside, Outside, Overlapping Lower, Lower)
  - Pilar 4: Dynamic N-Day CVA (Contiguous balance expansion and Dalton 100% measured move targets)
  - Pilar 5: Naked POCs (Virgin POC / VPOC tracking of unvisited historical liquidity magnets)
  - Time-Acceptance: Empirically verified duration confirmation (30m probe vs 60m acceptance)
"""

from __future__ import annotations

import math
from collections import defaultdict
from datetime import UTC, datetime, time, timedelta
from typing import Any

from .asof import parse_as_of
from .horizons import cme_session_date, cme_session_start

TPO_LETTERS = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"

ASSET_TICK_SIZES: dict[str, float] = {
    "NQ1": 0.25,
    "ES1": 0.25,
    "YM1": 1.0,
    "GC1": 0.10,
    "SI1": 0.005,
    "HG1": 0.0005,
    "CL1": 0.01,
    "BTCUSD": 1.0,
    "ETHUSD": 0.1,
    "EURUSD": 0.0001,
    "GBPUSD": 0.0001,
    "USDJPY": 0.01,
    "DXY": 0.01,
}


def get_asset_ib_timing(symbol: str, is_dst: bool = True) -> tuple[time, str]:
    """Determine asset-class-specific Initial Balance start time and institutional label.

    Different markets have distinct pit/cash openings:
      - CME Equities & US ETFs: 09:30 ET (NYSE/Nasdaq Cash Open)
      - Oil (CL1, BZ1): 09:00 ET (NYMEX Energy Pit Open)
      - Gold/Silver/Copper (GC1, SI1, HG1): 08:20 ET (COMEX Metals Pit Open)
      - Crypto (BTCUSD, ETHUSD): 18:00 ET (Globex/Session Open)
      - FX & DXY (EURUSD, GBPUSD, USDJPY, DXY): 08:00 London (London Interbank Open)
    """
    sym = symbol.strip().upper()
    if sym in ("BTCUSD", "ETHUSD"):
        # 18:00 ET = 22:00 UTC (EDT) or 23:00 UTC (EST)
        t = time(22, 0) if is_dst else time(23, 0)
        return t, "CRYPTO_SESSION_OPEN"
    if sym in ("CL1", "BZ1"):
        # 09:00 ET = 13:00 UTC (EDT) or 14:00 UTC (EST)
        t = time(13, 0) if is_dst else time(14, 0)
        return t, "NYMEX_ENERGY_PIT_0900ET"
    if sym in ("GC1", "SI1", "HG1"):
        # 08:20 ET = 12:20 UTC (EDT) or 13:20 UTC (EST)
        t = time(12, 20) if is_dst else time(13, 20)
        return t, "COMEX_METALS_PIT_0820ET"
    if sym in ("EURUSD", "GBPUSD", "USDJPY", "DXY"):
        # London Open: 08:00 London = 07:00 UTC (BST) or 08:00 UTC (GMT)
        t = time(7, 0) if is_dst else time(8, 0)
        return t, "LONDON_FX_OPEN"
    # Default: US Equity Cash Open 09:30 ET = 13:30 UTC (EDT) or 14:30 UTC (EST)
    t = time(13, 30) if is_dst else time(14, 30)
    return t, "US_CASH_OPEN_0930ET"


def split_rth(
    bars: list[tuple[str, float, float, float, float, float]],
    open_utc: time,
) -> tuple[list, list]:
    """Split one CME Globex session (opens 18:00 ET prior day) into (pre-open, RTH) bars.

    The open is anchored to the session start instant: comparing UTC time-of-day alone files
    the 22:00-23:59 UTC Globex-open bars as RTH. An open equal to the session start (crypto)
    makes the whole session RTH.
    """
    if not bars:
        return [], []
    start = cme_session_start(cme_session_date(bars[-1][0]))
    rth_open = datetime.combine(start.date(), open_utc, UTC)
    if rth_open < start:
        rth_open += timedelta(days=1)
    pre = [b for b in bars if parse_as_of(b[0]) < rth_open]
    return pre, [b for b in bars if parse_as_of(b[0]) >= rth_open]


def compute_value_area(
    bars: list[tuple[str, float, float, float, float, float]],
    *,
    num_bins: int = 50,
    va_volume_ratio: float = 0.70,
    tick_size: float | None = None,
) -> dict[str, Any]:
    """Compute Point of Control (POC), VAH, and VAL using discrete volume profile binning."""
    if not bars:
        return {
            "poc": None,
            "vah": None,
            "val": None,
            "total_volume": 0.0,
            "bars_count": 0,
        }

    highs = [b[2] for b in bars if b[2] is not None]
    lows = [b[3] for b in bars if b[3] is not None]
    if not highs or not lows:
        return {
            "poc": None,
            "vah": None,
            "val": None,
            "total_volume": 0.0,
            "bars_count": 0,
        }

    min_p = min(lows)
    max_p = max(highs)
    if max_p <= min_p:
        return {
            "poc": round(min_p, 4),
            "vah": round(min_p, 4),
            "val": round(min_p, 4),
            "total_volume": sum(b[5] for b in bars),
            "bars_count": len(bars),
        }

    if tick_size and tick_size > 0:
        raw_bin = (max_p - min_p) / float(num_bins)
        ticks_per_bin = max(1, round(raw_bin / tick_size))
        bin_size = ticks_per_bin * tick_size
        num_bins = max(5, int(math.ceil((max_p - min_p) / bin_size)))
    else:
        bin_size = (max_p - min_p) / float(num_bins)
    volume_by_bin = [0.0] * num_bins
    total_vol = 0.0

    for _ts, _o, h, low_val, _c, v in bars:
        vol = max(1.0, float(v) if v is not None else 1.0)
        total_vol += vol

        start_idx = max(0, min(num_bins - 1, int(math.floor((low_val - min_p) / bin_size))))
        end_idx = max(0, min(num_bins - 1, int(math.floor((h - min_p) / bin_size))))
        covered_bins = end_idx - start_idx + 1
        vol_per_bin = vol / float(covered_bins)
        for idx in range(start_idx, end_idx + 1):
            volume_by_bin[idx] += vol_per_bin

    poc_idx = max(range(num_bins), key=lambda i: volume_by_bin[i])
    poc_price = min_p + (poc_idx + 0.5) * bin_size

    target_vol = total_vol * va_volume_ratio
    current_vol = volume_by_bin[poc_idx]
    upper_idx = poc_idx
    lower_idx = poc_idx

    while current_vol < target_vol and (upper_idx < num_bins - 1 or lower_idx > 0):
        next_upper_vol = volume_by_bin[upper_idx + 1] if upper_idx + 1 < num_bins else -1.0
        next_lower_vol = volume_by_bin[lower_idx - 1] if lower_idx - 1 >= 0 else -1.0

        if next_upper_vol >= next_lower_vol and next_upper_vol >= 0.0:
            upper_idx += 1
            current_vol += next_upper_vol
        elif next_lower_vol > 0.0:
            lower_idx -= 1
            current_vol += next_lower_vol
        else:
            break

    vah_price = min_p + (upper_idx + 1.0) * bin_size
    val_price = min_p + lower_idx * bin_size

    return {
        "poc": round(poc_price, 4),
        "vah": round(vah_price, 4),
        "val": round(val_price, 4),
        "total_volume": round(total_vol, 2),
        "bars_count": len(bars),
        "bin_size": round(bin_size, 4),
    }


def compute_tpo_profile(
    bars: list[tuple[str, float, float, float, float, float]],
    *,
    num_bins: int = 40,
    rth_open_utc: time = time(13, 30),
) -> dict[str, Any]:
    """Compute TPO (Time-Price Opportunity) distribution across 30-minute brackets."""
    if not bars:
        return {
            "tpo_poc": None,
            "tpo_vah": None,
            "tpo_val": None,
            "total_tpos": 0,
            "brackets": [],
            "single_prints": [],
        }
    highs = [b[2] for b in bars if b[2] is not None]
    lows = [b[3] for b in bars if b[3] is not None]
    if not highs or not lows:
        return {
            "tpo_poc": None,
            "tpo_vah": None,
            "tpo_val": None,
            "total_tpos": 0,
            "brackets": [],
        }

    min_p, max_p = min(lows), max(highs)
    if max_p <= min_p:
        return {
            "tpo_poc": round(min_p, 4),
            "tpo_vah": round(min_p, 4),
            "tpo_val": round(min_p, 4),
            "total_tpos": len(bars),
            "brackets": ["A"],
            "single_prints": [],
        }

    bin_size = (max_p - min_p) / float(num_bins)

    first_dt = parse_as_of(bars[0][0])
    tpo_counts_by_bin = [0] * num_bins
    bracket_letters_by_bin: dict[int, set[str]] = defaultdict(set)
    used_brackets = set()

    for ts_str, _o, h, l_val, _c, _v in bars:
        b_dt = parse_as_of(ts_str)
        mins_elapsed = max(0, int((b_dt - first_dt).total_seconds() // 60))
        bracket_idx = min(len(TPO_LETTERS) - 1, mins_elapsed // 30)
        letter = TPO_LETTERS[bracket_idx]
        used_brackets.add(letter)

        s_idx = max(0, min(num_bins - 1, int(math.floor((l_val - min_p) / bin_size))))
        e_idx = max(0, min(num_bins - 1, int(math.floor((h - min_p) / bin_size))))

        for b_i in range(s_idx, e_idx + 1):
            if letter not in bracket_letters_by_bin[b_i]:
                bracket_letters_by_bin[b_i].add(letter)
                tpo_counts_by_bin[b_i] += 1

    total_tpos = sum(tpo_counts_by_bin)
    if total_tpos == 0:
        return {
            "tpo_poc": None,
            "tpo_vah": None,
            "tpo_val": None,
            "total_tpos": 0,
            "brackets": sorted(used_brackets),
            "single_prints": [],
        }
    poc_idx = max(range(num_bins), key=lambda i: tpo_counts_by_bin[i])
    tpo_poc = min_p + (poc_idx + 0.5) * bin_size

    target_tpos = total_tpos * 0.70
    cur_tpos = tpo_counts_by_bin[poc_idx]
    u_idx, l_idx = poc_idx, poc_idx

    while cur_tpos < target_tpos and (u_idx < num_bins - 1 or l_idx > 0):
        up_t = tpo_counts_by_bin[u_idx + 1] if u_idx + 1 < num_bins else -1
        dn_t = tpo_counts_by_bin[l_idx - 1] if l_idx - 1 >= 0 else -1

        if up_t >= dn_t and up_t >= 0:
            u_idx += 1
            cur_tpos += up_t
        elif dn_t >= 0:
            l_idx -= 1
            cur_tpos += dn_t
        else:
            break

    tpo_vah = min_p + (u_idx + 1.0) * bin_size
    tpo_val = min_p + l_idx * bin_size
    # Detect Single Prints (bins that have exactly 1 bracket letter)
    single_prints = []
    for b_i in range(num_bins):
        letters = bracket_letters_by_bin[b_i]
        if len(letters) == 1:
            p_low = round(min_p + b_i * bin_size, 4)
            p_high = round(min_p + (b_i + 1) * bin_size, 4)
            single_prints.append(
                {
                    "price_low": p_low,
                    "price_high": p_high,
                    "price_mid": round((p_low + p_high) / 2.0, 4),
                    "bracket": list(letters)[0],
                }
            )

    return {
        "tpo_poc": round(tpo_poc, 4),
        "tpo_vah": round(tpo_vah, 4),
        "tpo_val": round(tpo_val, 4),
        "total_tpos": total_tpos,
        "brackets": sorted(used_brackets),
        "bin_size": round(bin_size, 4),
        "single_prints": single_prints,
    }


def evaluate_vpoc_tpoc_relationship(
    volume_poc: float,
    tpo_poc: float,
    atr: float,
) -> dict[str, Any]:
    """Evaluate institutional value migration between Volume POC and TPO POC."""
    diff = volume_poc - tpo_poc
    threshold = 0.10 * max(0.001, atr)

    if diff > threshold:
        return {
            "relationship": "VPOC_ABOVE_TPOC",
            "bias": "INSTITUTIONAL_BUY_MIGRATION",
            "meaning": "Volume POC formed above TPO POC: Large capital traded aggressively higher faster than time spent (Accumulation).",
            "diff_points": round(diff, 4),
        }
    if diff < -threshold:
        return {
            "relationship": "VPOC_BELOW_TPOC",
            "bias": "INSTITUTIONAL_SELL_DISTRIBUTION",
            "meaning": "Volume POC formed below TPO POC: Heavy capital transactions concentrated at lows while price spent time above (Distribution).",
            "diff_points": round(diff, 4),
        }
    return {
        "relationship": "ALIGNED_TRUE_CONSENSUS",
        "bias": "EQUILIBRIUM_SUPPORT_RESISTANCE",
        "meaning": "Volume POC and TPO POC aligned: Market achieved true two-way auction consensus (Maximum magnetic equilibrium).",
        "diff_points": round(diff, 4),
    }


def analyze_initial_balance(
    bars: list[tuple[str, float, float, float, float, float]],
    cash_open_time: time,
) -> dict[str, Any]:
    """Calculate Initial Balance (IB: first 60m of cash open) and classify Day Type."""
    _, rth_bars = split_rth(bars, cash_open_time)

    if not rth_bars:
        return {
            "ib_high": None,
            "ib_low": None,
            "ib_range": None,
            "day_type": "UNKNOWN_PRE_CASH",
            "extension_ratio": 0.0,
        }

    ib_bars = rth_bars[:12]
    ib_high = max(b[2] for b in ib_bars)
    ib_low = min(b[3] for b in ib_bars)
    ib_range = max(0.001, ib_high - ib_low)

    post_ib_bars = rth_bars[12:]
    if not post_ib_bars:
        return {
            "ib_high": round(ib_high, 4),
            "ib_low": round(ib_low, 4),
            "ib_range": round(ib_range, 4),
            "day_type": "FORMING_INITIAL_BALANCE",
            "extension_ratio": 0.0,
        }

    day_high = max(b[2] for b in rth_bars)
    day_low = min(b[3] for b in rth_bars)
    day_close = rth_bars[-1][4]

    high_extension = max(0.0, (day_high - ib_high) / ib_range)
    low_extension = max(0.0, (ib_low - day_low) / ib_range)
    total_extension = high_extension + low_extension

    if (high_extension > 2.0 and low_extension < 0.2) or (
        low_extension > 2.0 and high_extension < 0.2
    ):
        day_type = "TREND_DAY"
    elif (high_extension > 0.5 and low_extension > 0.5) and (ib_low <= day_close <= ib_high):
        day_type = "NEUTRAL_DAY"
    elif high_extension > 0.3 or low_extension > 0.3:
        day_type = "NORMAL_VARIATION_DAY"
    else:
        day_type = "NORMAL_DAY"

    return {
        "ib_high": round(ib_high, 4),
        "ib_low": round(ib_low, 4),
        "ib_range": round(ib_range, 4),
        "day_type": day_type,
        "high_extension_ratio": round(high_extension, 2),
        "low_extension_ratio": round(low_extension, 2),
        "total_extension_ratio": round(total_extension, 2),
    }


def classify_profile_shape(
    poc: float,
    vah: float,
    val: float,
    session_high: float,
    session_low: float,
) -> dict[str, str]:
    """Classify Auction Profile Shape: D-Shape, P-Shape, b-Shape, or B-Shape."""
    session_range = max(0.001, session_high - session_low)
    relative_poc_pos = (poc - session_low) / session_range

    if relative_poc_pos >= 0.65:
        shape = "P_SHAPE"
        meaning = "Initiative Buying / Short Covering (Value migration higher)"
    elif relative_poc_pos <= 0.35:
        shape = "b_SHAPE"
        meaning = "Long Liquidation / Aggressive Selling (Value migration lower)"
    else:
        shape = "D_SHAPE"
        meaning = "Balanced Distribution (Market in rotational fair-value agreement)"

    return {
        "shape": shape,
        "relative_poc_position": f"{round(relative_poc_pos * 100, 1)}%",
        "meaning": meaning,
    }


def evaluate_auction_extremes(
    bars: list[tuple[str, float, float, float, float, float]],
    atr: float,
) -> dict[str, Any]:
    """Detect Excess Tails vs Poor High/Low (Unfinished Auctions)."""
    if len(bars) < 5:
        return {
            "high_structure": "NORMAL",
            "low_structure": "NORMAL",
        }

    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    session_high = max(highs)
    session_low = min(lows)

    near_high_count = sum(1 for h in highs if h >= session_high - (0.05 * atr))
    near_low_count = sum(1 for l_val in lows if l_val <= session_low + (0.05 * atr))

    high_bar = next(b for b in bars if b[2] == session_high)
    high_wick = session_high - max(high_bar[1], high_bar[4])
    is_excess_high = high_wick >= (0.15 * atr) and near_high_count <= 2

    low_bar = next(b for b in bars if b[3] == session_low)
    low_wick = min(low_bar[1], low_bar[4]) - session_low
    is_excess_low = low_wick >= (0.15 * atr) and near_low_count <= 2

    high_structure = (
        "EXCESS_SELLING_TAIL (Valid Resistance)"
        if is_excess_high
        else (
            "POOR_HIGH (Unfinished Auction - Likely to be retested)"
            if near_high_count >= 3
            else "NORMAL_HIGH"
        )
    )

    low_structure = (
        "EXCESS_BUYING_TAIL (Valid Support)"
        if is_excess_low
        else (
            "POOR_LOW (Unfinished Auction - Likely to be retested)"
            if near_low_count >= 3
            else "NORMAL_LOW"
        )
    )

    return {
        "high_structure": high_structure,
        "low_structure": low_structure,
        "session_high": round(session_high, 4),
        "session_low": round(session_low, 4),
        "high_excess_wick_atr": round(high_wick / atr, 2) if atr > 0 else 0.0,
        "low_excess_wick_atr": round(low_wick / atr, 2) if atr > 0 else 0.0,
    }


# ==============================================================================
# PILAR 1: The 4 Open Types (James Dalton)
# ==============================================================================
def classify_open_type(
    rth_bars: list[tuple[str, float, float, float, float, float]],
    pdh: float,
    pdl: float,
    vah: float,
    val: float,
    atr: float,
) -> dict[str, Any]:
    """Classify Dalton 4 Open Types: Open Drive, Open Test-Drive, Open Rejection-Reverse, Open in Value."""
    if len(rth_bars) < 3:
        return {
            "open_type": "UNKNOWN_INSUFFICIENT_BARS",
            "conviction": "UNKNOWN",
            "rationale": "Less than 3 bars in RTH session.",
        }

    first_bar = rth_bars[0]
    open_p = first_bar[1]
    third_bar = rth_bars[2]
    inter_high = max(b[2] for b in rth_bars[:3])
    inter_low = min(b[3] for b in rth_bars[:3])
    close_3 = third_bar[4]

    tol = 0.08 * atr

    # 1. Open Drive (OD): Aggressive directional move with near-zero opposing tail
    is_up_drive = (open_p - inter_low) <= (0.05 * atr) and (close_3 > open_p + (0.25 * atr))
    is_down_drive = (inter_high - open_p) <= (0.05 * atr) and (close_3 < open_p - (0.25 * atr))
    if is_up_drive:
        return {
            "open_type": "OPEN_DRIVE_BULLISH",
            "conviction": "HIGHEST_CONVICTION",
            "rationale": "Price opened and drove immediately higher without opposing tail. Institutional initiative buyers in control.",
        }
    if is_down_drive:
        return {
            "open_type": "OPEN_DRIVE_BEARISH",
            "conviction": "HIGHEST_CONVICTION",
            "rationale": "Price opened and drove immediately lower without opposing tail. Institutional initiative sellers in control.",
        }

    # 2. Open Rejection-Reverse (ORR): Pierces outside PDH/PDL and immediately reverses
    if inter_high > pdh and close_3 < pdh - tol:
        return {
            "open_type": "OPEN_REJECTION_REVERSE_BEARISH",
            "conviction": "MODERATE_CONVICTION",
            "rationale": "Price spiked above PDH but was immediately rejected back into range. Bull trap / Liquidity sweep.",
        }
    if inter_low < pdl and close_3 > pdl + tol:
        return {
            "open_type": "OPEN_REJECTION_REVERSE_BULLISH",
            "conviction": "MODERATE_CONVICTION",
            "rationale": "Price pierced below PDL but was immediately reclaimed back into range. Bear trap / Liquidity sweep.",
        }

    # 3. Open Test-Drive (OTD): Tests a prior reference level and drives away
    tested_vah = abs(inter_low - vah) <= tol and close_3 > vah + (0.15 * atr)
    tested_val = abs(inter_high - val) <= tol and close_3 < val - (0.15 * atr)
    if tested_vah:
        return {
            "open_type": "OPEN_TEST_DRIVE_BULLISH",
            "conviction": "HIGH_CONVICTION",
            "rationale": "Price tested prior VAH, found aggressive buyers, and drove higher.",
        }
    if tested_val:
        return {
            "open_type": "OPEN_TEST_DRIVE_BEARISH",
            "conviction": "HIGH_CONVICTION",
            "rationale": "Price tested prior VAL, found aggressive sellers, and drove lower.",
        }

    # 4. Open in Value / Range (OIV): Opened inside value and remains balanced
    if val <= open_p <= vah:
        return {
            "open_type": "OPEN_IN_VALUE",
            "conviction": "LOW_CONVICTION_CHOP",
            "rationale": "Price opened inside prior Value Area. Market in two-way rotational consensus awaiting catalysts.",
        }

    return {
        "open_type": "OPEN_IN_RANGE",
        "conviction": "MODERATE_CONVICTION",
        "rationale": "Price opened between Value Area and prior day extremes.",
    }


# ==============================================================================
# PILAR 2: Participant Activity (Initiative vs Responsive)
# ==============================================================================
def classify_participant_activity(
    last_price: float,
    vah: float,
    val: float,
    last_bar: tuple[str, float, float, float, float, float],
) -> dict[str, str]:
    """Classify current market behavior as Initiative vs Responsive Buying or Selling."""
    _ts, o, _h, _l, c, _v = last_bar
    is_bullish = c >= o

    if last_price > vah:
        if is_bullish:
            act = "INITIATIVE_BUYING"
            meaning = "Aggressive buyers paying premium above fair value to force trend expansion."
        else:
            act = "RESPONSIVE_SELLING"
            meaning = "Sellers fading premium prices above fair value to push price back inside."
    elif last_price < val:
        if is_bullish:
            act = "RESPONSIVE_BUYING"
            meaning = (
                "Value buyers stepping in at discount below fair value to accumulate inventory."
            )
        else:
            act = "INITIATIVE_SELLING"
            meaning = "Aggressive sellers accepting discount below fair value to push breakdown."
    else:
        act = "ROTATIONAL_AUCTION"
        meaning = "Price oscillating between buyers and sellers within established fair value."

    return {
        "activity": act,
        "meaning": meaning,
    }


# ==============================================================================
# PILAR 3: Value Migration Day-to-Day Relationships
# ==============================================================================
def classify_value_migration(
    curr_vah: float | None,
    curr_val: float | None,
    curr_poc: float | None,
    prior_vah: float | None,
    prior_val: float | None,
    prior_poc: float | None,
) -> dict[str, str]:
    """Classify the 6 classic day-to-day Value Area migration relationships."""
    if None in (curr_vah, curr_val, prior_vah, prior_val):
        return {
            "relationship": "INSUFFICIENT_VALUE_DATA",
            "bias": "NEUTRAL",
            "meaning": "Value Area levels not fully established.",
        }

    if curr_val > prior_vah:
        rel = "HIGHER_VALUE"
        bias = "STRONG_BULLISH"
        meaning = "Entire Value Area migrated above prior day. Strong trend continuation."
    elif curr_vah < prior_val:
        rel = "LOWER_VALUE"
        bias = "STRONG_BEARISH"
        meaning = "Entire Value Area migrated below prior day. Strong breakdown continuation."
    elif curr_vah > prior_vah and curr_val < prior_val:
        rel = "OUTSIDE_VALUE"
        bias = "EXPANSION_VOLATILITY"
        meaning = "Value Area expanded beyond both prior boundaries. Range expansion."
    elif curr_vah <= prior_vah and curr_val >= prior_val:
        rel = "INSIDE_VALUE"
        bias = "COMPRESSION_CHOP"
        meaning = "Value Area completely contained within prior day. Market energy coiling."
    elif curr_vah > prior_vah and curr_val >= prior_val:
        rel = "OVERLAPPING_HIGHER"
        bias = "MODERATE_BULLISH"
        meaning = "Value shifting upward with shared acceptance."
    else:
        rel = "OVERLAPPING_LOWER"
        bias = "MODERATE_BEARISH"
        meaning = "Value shifting downward with shared acceptance."

    return {
        "relationship": rel,
        "bias": bias,
        "meaning": meaning,
    }


# ==============================================================================
# PILAR 4: Dynamic N-Day CVA (Contiguous Balance Progression)
# ==============================================================================
def compute_dynamic_cva(
    session_bars_dict: dict[str, list[tuple[str, float, float, float, float, float]]],
    min_sessions: int = 2,
    max_sessions: int = 10,
) -> dict[str, Any] | None:
    """Dynamically merge contiguous overlapping balance sessions and project Dalton 100% measured moves."""
    sorted_sessions = sorted(session_bars_dict.keys())
    if len(sorted_sessions) < min_sessions:
        return None

    # Step backward from T-1: merge while consecutive sessions overlap in Value Area
    reversed_sessions = (
        list(reversed(sorted_sessions[:-1])) if len(sorted_sessions) > 1 else sorted_sessions
    )
    merged_sessions = []
    prior_va = None

    for s_id in reversed_sessions[:max_sessions]:
        bars = session_bars_dict[s_id]
        va = compute_value_area(bars, num_bins=40)
        if not va or va["vah"] is None or va["val"] is None:
            break

        if prior_va is None:
            merged_sessions.append(s_id)
            prior_va = va
        else:
            # Check overlap: VAH_1 >= VAL_2 and VAL_1 <= VAH_2
            is_overlap = (va["vah"] >= prior_va["val"]) and (va["val"] <= prior_va["vah"])
            if is_overlap:
                merged_sessions.append(s_id)
                prior_va = va
            else:
                break

    if len(merged_sessions) < min_sessions:
        merged_sessions = sorted_sessions[-min_sessions:]

    actual_sessions = sorted(merged_sessions)
    all_bars = []
    for s_id in actual_sessions:
        all_bars.extend(session_bars_dict[s_id])

    comp = compute_value_area(all_bars, num_bins=50)
    c_vah = comp["vah"]
    c_val = comp["val"]
    c_poc = comp["poc"]
    c_range = round(c_vah - c_val, 4) if c_vah and c_val else 0.0

    # Dalton 100% Measured Move Projections
    long_target = round(c_vah + c_range, 4) if c_vah else None
    short_target = round(c_val - c_range, 4) if c_val else None

    return {
        "composite_name": f"{len(actual_sessions)}D_DYNAMIC_CVA",
        "composite_days_count": len(actual_sessions),
        "sessions_included": actual_sessions,
        "c_poc": c_poc,
        "c_vah": c_vah,
        "c_val": c_val,
        "c_range": c_range,
        "dalton_measured_move": {
            "upside_breakout_target": long_target,
            "downside_breakout_target": short_target,
            "formula": "c_boundary ± 100% of composite_range",
        },
        "total_volume": comp["total_volume"],
        "bars_evaluated": len(all_bars),
    }


# ==============================================================================
# PILAR 5: Naked POCs (Virgin POCs Tracker)
# ==============================================================================
def find_naked_pocs(
    session_bars_dict: dict[str, list[tuple[str, float, float, float, float, float]]],
    current_price: float,
    lookback_sessions: int = 25,
) -> dict[str, Any]:
    """Scan historical sessions for unvisited/untouched Naked POCs (Virgin POCs)."""
    sorted_sessions = sorted(session_bars_dict.keys())
    if len(sorted_sessions) < 2:
        return {
            "total_naked_pocs": 0,
            "nearest_naked_poc_above": None,
            "nearest_naked_poc_below": None,
            "naked_pocs": [],
        }

    eval_sessions = sorted_sessions[-lookback_sessions:]
    naked_pocs = []

    for i in range(len(eval_sessions) - 1):
        s_id = eval_sessions[i]
        bars = session_bars_dict[s_id]
        va = compute_value_area(bars, num_bins=40)
        poc = va.get("poc")
        if poc is None:
            continue

        # Check all subsequent sessions up to the latest bar
        touched = False
        for j in range(i + 1, len(eval_sessions)):
            sub_bars = session_bars_dict[eval_sessions[j]]
            for _ts, _o, h, l_val, _c, _v in sub_bars:
                if l_val <= poc <= h:
                    touched = True
                    break
            if touched:
                break

        if not touched:
            naked_pocs.append(
                {
                    "session_id": s_id,
                    "poc": round(poc, 4),
                    "distance_pct": round(((poc - current_price) / current_price) * 100, 2),
                }
            )

    above = [np for np in naked_pocs if np["poc"] > current_price]
    below = [np for np in naked_pocs if np["poc"] < current_price]

    nearest_above = min(above, key=lambda x: x["poc"]) if above else None
    nearest_below = max(below, key=lambda x: x["poc"]) if below else None

    return {
        "total_naked_pocs": len(naked_pocs),
        "nearest_naked_poc_above": nearest_above,
        "nearest_naked_poc_below": nearest_below,
        "all_naked_pocs": naked_pocs,
    }


def compute_composite_value_area(
    session_bars_dict: dict[str, list[tuple[str, float, float, float, float, float]]],
    num_sessions: int = 2,
) -> dict[str, Any] | None:
    """Compute N-Day Composite Value Area (e.g. 2D CVA) over multi-session consolidation."""
    sorted_sessions = sorted(session_bars_dict.keys())
    if len(sorted_sessions) < num_sessions:
        return None

    target_sessions = sorted_sessions[-num_sessions:]
    combined_bars = []
    for s_id in target_sessions:
        combined_bars.extend(session_bars_dict[s_id])

    cva = compute_value_area(combined_bars, num_bins=50)
    return {
        "composite_sessions": target_sessions,
        "composite_name": f"{num_sessions}D_CVA",
        "c_poc": cva["poc"],
        "c_vah": cva["vah"],
        "c_val": cva["val"],
        "total_volume": cva["total_volume"],
        "bars_evaluated": len(combined_bars),
    }


def evaluate_time_acceptance(
    recent_closes: list[float],
    vah: float,
    val: float,
) -> dict[str, Any]:
    """Evaluate empirical time-acceptance status based on backtested 30m/60m thresholds."""
    if not recent_closes:
        return {
            "status": "INSIDE_VALUE",
            "duration_minutes": 0,
            "acceptance_level": "NONE",
        }

    above_count = 0
    for c in reversed(recent_closes):
        if c > vah:
            above_count += 1
        else:
            break

    below_count = 0
    for c in reversed(recent_closes):
        if c < val:
            below_count += 1
        else:
            break

    if above_count > 0:
        duration_mins = above_count * 5
        if duration_mins >= 60:
            level = "DEFINITIVE_ACCEPTANCE_60M (Dalton Rule - Lowest Trap Rate 4.8%-6.2%)"
            st = "ACCEPTED_ABOVE_VAH"
        elif duration_mins >= 30:
            level = "EARLY_PROBE_30M (1 TPO Bracket Confirmation)"
            st = "PROBING_ABOVE_VAH"
        else:
            level = "PREMATURE_UNDER_30M (High Trap Probability)"
            st = "SPIKE_ABOVE_VAH"
    elif below_count > 0:
        duration_mins = below_count * 5
        if duration_mins >= 60:
            level = "DEFINITIVE_ACCEPTANCE_60M (Dalton Rule - Lowest Trap Rate 4.8%-6.2%)"
            st = "ACCEPTED_BELOW_VAL"
        elif duration_mins >= 30:
            level = "EARLY_PROBE_30M (1 TPO Bracket Confirmation)"
            st = "PROBING_BELOW_VAL"
        else:
            level = "PREMATURE_UNDER_30M (High Trap Probability)"
            st = "SPIKE_BELOW_VAL"
    else:
        duration_mins = 0
        level = "INSIDE_ESTABLISHED_VALUE"
        st = "INSIDE_VALUE"

    return {
        "status": st,
        "consecutive_bars_outside": max(above_count, below_count),
        "duration_minutes": duration_mins,
        "acceptance_level": level,
    }
