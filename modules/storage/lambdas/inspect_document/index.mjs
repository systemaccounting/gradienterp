/**
 * inspect_document — read a document's contents into structured fields (see ../../AGENTS.md).
 *
 * The ONE operation in modules/storage that opens an object and reads its bytes (manage_storage
 * organizes objects but never reads contents). For a receipt/invoice it runs Textract AnalyzeExpense —
 * deterministic, with a per-field confidence score — so the agent books the total without ever
 * eyeballing the image: the model that classifies the expense never sees the pixels, so a hallucinated
 * total can't reach the ledger. Textract's "$84.23" strings are parsed to floats HERE (translate at the
 * boundary) — the agent receives `total: 84.23` and hands it straight to post_journal_entry.
 *
 * Bytes never transit the agent: the lambda reads + decrypts the object and passes the bytes to Textract
 * in-account; the agent receives only the structured fields. The result is cached as an `inspection`
 * annotation on the object (alongside `caption`), so it rides CopyObject when op=move relocates a
 * submission → its semantic key, and a later read-back returns the cache instead of re-invoking Textract.
 *
 * Gateway tool: inspect an object by `key` — a portal submission file (submissions/…) before
 * filing, or an already-filed document ("read it back").
 */
import {
  S3Client, GetObjectCommand, PutObjectAnnotationCommand, GetObjectAnnotationCommand, NoSuchAnnotation,
} from "@aws-sdk/client-s3";
import { TextractClient, AnalyzeExpenseCommand } from "@aws-sdk/client-textract";

const s3 = new S3Client({});
const textract = new TextractClient({});
const BUCKET = process.env.STORAGE_BUCKET;
const INSPECTION = "inspection"; // annotation caching the extracted fields (alongside `caption`)

const resp = (body, statusCode = 200) => ({ statusCode, body: JSON.stringify(body) });

// "$84.23" / "$1,234.56" → 84.23 / 1234.56 ; null if there's no number. Parse at the boundary so the
// agent (and post_journal_entry) get a float, never a "$" string.
const money = (t) => {
  if (t == null) return null;
  const n = parseFloat(String(t).replace(/[^0-9.\-]/g, ""));
  return Number.isFinite(n) ? n : null;
};
const clean = (t) => (t == null ? null : String(t).replace(/\s+/g, " ").trim());

// Textract emits duplicate SummaryFields (e.g. VENDOR_NAME from both the header and the "Thank you"
// footer) — keep the highest-confidence detection per type.
function summarize(fields) {
  const best = {};
  for (const f of fields || []) {
    const type = f.Type?.Text;
    const v = f.ValueDetection || {};
    if (!type) continue;
    if (!best[type] || (v.Confidence || 0) > best[type].confidence) {
      best[type] = { text: v.Text, confidence: v.Confidence || 0 };
    }
  }
  return best;
}

export function mapExpense(analyzeResp) {
  const doc = (analyzeResp.ExpenseDocuments || [])[0] || {};
  const s = summarize(doc.SummaryFields);
  const val = (t) => s[t]?.text ?? null;

  const line_items = [];
  for (const g of doc.LineItemGroups || []) {
    for (const li of g.LineItems || []) {
      const row = {};
      for (const x of li.LineItemExpenseFields || []) {
        const t = x.Type?.Text, txt = x.ValueDetection?.Text;
        if (t === "QUANTITY") row.quantity = clean(txt);
        else if (t === "ITEM") row.item = clean(txt);
        else if (t === "PRICE") row.price = money(txt);
      }
      if (row.item || row.price != null) line_items.push(row);
    }
  }

  const vendorRaw = val("VENDOR_NAME") ?? val("NAME");
  const vendor = vendorRaw ? clean(vendorRaw).replace(/[!.,;:]+$/, "") : null; // "Staples!" → "Staples"

  return {
    fields: {
      vendor,
      total: money(val("TOTAL")),
      tax: money(val("TAX")),
      subtotal: money(val("SUBTOTAL")),
      date: clean(val("INVOICE_RECEIPT_DATE")),
      line_items,
    },
    // gate on the TOTAL's confidence — the number that hits the ledger
    confidence: s.TOTAL?.confidence ?? 0,
  };
}

async function readCache(key) {
  try {
    const r = await s3.send(new GetObjectAnnotationCommand({ Bucket: BUCKET, Key: key, AnnotationName: INSPECTION }));
    return JSON.parse(await r.AnnotationPayload.transformToString());
  } catch (e) {
    if (e instanceof NoSuchAnnotation || e.name === "NoSuchAnnotation") return null; // uploaded but un-inspected
    throw e;
  }
}

async function writeCache(key, result) {
  await s3.send(new PutObjectAnnotationCommand({
    Bucket: BUCKET, Key: key, AnnotationName: INSPECTION,
    AnnotationPayload: new TextEncoder().encode(JSON.stringify(result)), // UTF-8, ≤ 1 MiB; inherits SSE-KMS
  }));
}

async function inspectReceipt(key) {
  const obj = await s3.send(new GetObjectCommand({ Bucket: BUCKET, Key: key }));
  const bytes = await obj.Body.transformToByteArray();
  const r = await textract.send(new AnalyzeExpenseCommand({ Document: { Bytes: bytes } }));
  return mapExpense(r);
}

export const handler = async (event) => {
  const e = typeof event.body === "string" ? JSON.parse(event.body) : event;
  const key = e.key;
  if (!key) return resp({ error: "provide key" }, 400);
  const schema = e.schema || "receipt";
  if (schema !== "receipt") return resp({ error: `schema ${JSON.stringify(schema)} not supported yet (receipt only)` }, 400);

  if (!e.refresh) {
    const cached = await readCache(key);
    if (cached) return resp({ ...cached, source: "textract", cached: true });
  }

  const result = await inspectReceipt(key); // { fields, confidence }
  await writeCache(key, result);
  return resp({ ...result, source: "textract", cached: false });
};
