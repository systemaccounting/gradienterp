// the payer's landing (paid.html): a file, so the page's CSP allows no inline script
const paid = new URLSearchParams(location.search).get("paid");
document.getElementById("title").textContent = paid ? "Payment received" : "No payment was taken";
document.getElementById("note").textContent = paid
  ? "The business will see it on invoice " + paid + ". You can close this window."
  : "The payment was cancelled. The link you were sent still works when you're ready.";
