# KR Choksey → GnuCash — Usage Guide

This guide covers the **KRChoksey** skills (under the **GnuCash** tab) that turn a KR Choksey broker statement into importable GnuCash entries. There are three sub-tabs, run in order:

1. **KRChoksey** (Part I) — simplify the annual ledger statement PDF into a clean *Simplified Ledger* workbook.
2. **Reconcile** (Part II) — match the contract notes (SLBM memos + trade notes) against that ledger and produce a *Bills* workbook.
3. **GnuCash Import** (Part III) — convert the *Bills* workbook into importable GnuCash CSVs.

All three run **deterministically with no LLM** (fast and reproducible).

## Part III — GnuCash Import

**Inputs**

- *Part II Bills workbook* — picked from the dropdown of prior `*-KRC-Bills-Recon.xlsx` outputs.
- *GnuCash book* — the account holder's `.gnucash` file (used for account paths and FIFO cost basis).

**Outputs** (a folder containing):

- `Purchase.csv` — one transaction per purchase: **Dr** the security account (with the share quantity), **Cr** the KR Choksey broker account, for the Net/Bill Amount.
- `SLBM.csv` — one transaction per SLBM bill: **Dr** KR Choksey broker, **Cr** *Income from SLBS* (the net lending proceeds, offered to tax as income).
- `Sale.csv` — a multi-split transaction per sale: **Dr** broker (sale proceeds), **Cr** the security at **FIFO cost basis** (with negative shares), **Cr** *Long Term* and/or *Short Term Capital Gain* (the gain, apportioned per FIFO lot; a loss appears as a positive/debit amount).
- `Review.csv` — only if something needs your attention (a security name that couldn't be matched to an account, or a sale with insufficient FIFO lots).

Every transaction's `Amount` column sums to zero, so each imports as a balanced GnuCash transaction.

## The configuration file

Behaviour you can change lives in an editable YAML file:

> **`Data/settings/krc_gnucash_config.yaml`**

It is created automatically on first run if missing. Options:

- `long_term_threshold_months` — holding period (in months) separating **Long Term** from **Short Term** capital gains. A FIFO lot held *strictly longer* than this at the time of sale is Long Term. Default `12`. **This is not hard-coded — edit it here.**
- `accounts` — the destination account paths in your book (`broker`, `slbs_income`, `ltcg`, `stcg`). Change these if your account names differ.
- `currency` — the currency for all splits (default `INR`).
- `security_aliases` — force a specific Bills *Security* name to a specific GnuCash stock account path, overriding the automatic name match. Use this whenever a security lands in `Review.csv`, for example:

  ```yaml
  security_aliases:
    "SUZLON ENERGY LTD.": "Assets:Investments:Shares:Suzlon Energy Limited"
  ```

## How FIFO cost basis works

For a **sale**, the skill reads the security's prior purchase lots (date, shares, price) from your `.gnucash` book — and any earlier purchase in the same run — and consumes them **oldest-first (FIFO)**. The cost basis is the sum of (shares × lot price). The gain (proceeds − cost basis) is apportioned across the consumed lots: lots older than the threshold contribute to **Long Term Capital Gain**, the rest to **Short Term Capital Gain**. If a sale straddles the threshold, both gain lines appear.

If a sale needs more shares than the book holds, it is sent to `Review.csv` rather than guessed.

## Importing the CSVs into GnuCash

For each of `Purchase.csv`, `SLBM.csv`, `Sale.csv`:

1. **File → Import → Import Transactions from CSV…** and select the file.
2. Tick the **Multi-split** box (rows of one transaction share the same *Transaction ID*).
3. Set **Skip rows: 1** (the header) and the **Date Format to `y-m-d`** (the CSVs use ISO `YYYY-MM-DD`).
4. Map these directly: *Date → Date*, *Transaction ID → Transaction ID*, *Number → Num* (the visible reference number), *Description → Description*, *Account → Account*, *Currency → Transaction Commodity*. Then choose **one** value/quantity pairing (there is no *Shares* column type):
   - **Recommended — *Value → Value* and *Amount → Amount*.** For a stock row *Amount* is the exact share quantity; GnuCash derives price = Value ÷ Amount. Leave *Price* unmapped.
   - **Fallback — *Value → Value* and *Price → Price*** (leave *Amount* unmapped). GnuCash back-calculates shares = Value ÷ Price. Use this only if your build doesn't take the quantity from *Amount*.

   Map only **one** of the two pairings, not all three columns.

   > **`SLBM.csv` is simpler** — no securities, so it has a single signed **Amount** column (no Value/Price). For it, just map *Amount → Amount* (plus Date / Transaction ID / Number / Description / Account / Currency). The value/quantity pairing above applies only to **`Purchase.csv`** and **`Sale.csv`**.
5. Review the preview (every transaction should balance on *Value*) and import.

> If a build of GnuCash imports the signs reversed, map the *Amount* column to **Amount (Negated)** instead.

## Notes

- SLBM is a netting game — only the net proceeds are booked (to *Income from SLBS*); the individual borrow/return legs are not separate stock movements.
- Purchases and sales are standalone single-security rows in the Bills sheet; multi-line netting only occurs for SLBM.
