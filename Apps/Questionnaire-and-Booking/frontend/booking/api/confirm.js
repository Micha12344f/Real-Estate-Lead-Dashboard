// Vercel serverless proxy: POST /api/confirm
// Proxies to the configured backend webhook (server-to-server).
module.exports = async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") return res.status(200).end();
  if (req.method !== "POST") return res.status(405).json({ status: "error", message: "Method not allowed." });

  try {
    const body = req.body || {};
    const token = (body.token || req.query.token || "").trim();
    if (!token) return res.status(400).json({ status: "invalid_token", message: "Missing token." });

    const baseUrl = String(process.env.BOOKING_BASE_URL || "").trim().replace(/\/+$/, "");
    const apiKey = String(process.env.X_TVE_API_KEY || "").trim();
    if (!baseUrl || !apiKey) return res.status(503).json({ status: "configuration_error", message: "Booking backend URL and webhook API key must be configured on the server." });
    const url = `${baseUrl}/thames-valley-estates/booking-confirm`;

    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json", "X-TVE-Auth": apiKey },
      body: JSON.stringify({ ...body, token })
    });

    const responseText = await response.text();
    let data;
    try { data = JSON.parse(responseText); } catch { data = null; }

    if (data && data.status === "error" && !data.message) {
      data.message = "Your viewing request could not be saved. Please try again or check your email for an acknowledgement.";
    }

    res.status(response.status).json(data || { status: "error", message: responseText || "Your viewing request could not be saved. Please try again." });
  } catch (err) {
    res.status(502).json({ status: "error", message: err.message || "Confirm proxy failed." });
  }
};
