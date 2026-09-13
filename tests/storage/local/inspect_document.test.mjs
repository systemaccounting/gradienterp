// Pure-logic test for storage/inspect_document — the Textract → fields mapping, run against the REAL
// AnalyzeExpense response for tmp/staples-receipt.jpg (a phone photo of a screen: glare, angle, moiré).
// Fixture is the live Textract output (SummaryFields + LineItemGroups verbatim), not a fabricated shape.
// The live S3 + Textract path wants an integ test against the applied bucket. Run: `bash scripts/test.sh --module storage`.
import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { mapExpense } from "../../../modules/storage/lambdas/inspect_document/index.mjs";

const real = JSON.parse(readFileSync(new URL("./fixtures/analyze_expense_staples.json", import.meta.url)));

test("mapExpense pulls the receipt totals as floats from the real Textract response", () => {
  const { fields, confidence } = mapExpense(real);
  assert.equal(fields.total, 84.23); // number, not "$84.23" — parsed at the boundary
  assert.equal(fields.tax, 6.6);
  assert.equal(fields.subtotal, 77.63);
  assert.equal(fields.date, "2026-06-14");
  assert.equal(typeof fields.total, "number");
  assert.ok(confidence >= 99, `TOTAL confidence ${confidence} should be high on a clean receipt`);
});

test("mapExpense dedups the vendor and strips trailing punctuation", () => {
  // Textract emits VENDOR_NAME twice: "STAPLES" (header) and "Staples!" (footer) — one clean value out.
  const { fields } = mapExpense(real);
  assert.match(fields.vendor, /^Staples$/i);
});

test("mapExpense extracts the line items with numeric prices", () => {
  const { fields } = mapExpense(real);
  assert.equal(fields.line_items.length, 4);
  const paper = fields.line_items.find((r) => /printer paper/i.test(r.item));
  assert.equal(paper.quantity, "2");
  assert.equal(paper.price, 12.98);
  assert.equal(typeof paper.price, "number");
});

test("mapExpense is graceful on an empty/unreadable response", () => {
  const { fields, confidence } = mapExpense({ ExpenseDocuments: [] });
  assert.equal(fields.total, null);
  assert.equal(fields.line_items.length, 0);
  assert.equal(confidence, 0);
});
