// Server-side proxy keeps the webhook credential out of browser responses.
module.exports = async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") return res.status(200).end();
  if (req.method !== "POST") return res.status(405).json({ status: "error", message: "Method not allowed." });

  const target = String(process.env.QUESTIONNAIRE_SUBMIT_BASE_URL || "").trim();
  const apiKey = String(process.env.X_TVE_API_KEY || "").trim();
  if (!target || !apiKey) {
    return res.status(503).json({ status: "configuration_error", message: "Questionnaire submission is not configured on the server." });
  }

  try {
    const response = await fetch(target, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json", "X-TVE-Auth": apiKey },
      body: JSON.stringify((req.body && req.body.payload) || {})
    });
    const responseText = await response.text();
    let data;
    try { data = JSON.parse(responseText); } catch { data = null; }
    return res.status(response.status).json(data || { status: "error", message: responseText || "Questionnaire submission failed." });
  } catch (_err) {
    return res.status(502).json({ status: "error", message: "Questionnaire submission proxy failed." });
  }
};
