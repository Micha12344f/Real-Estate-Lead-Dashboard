// Vercel serverless proxy: POST /api/booking-guest
// Proxies to the configured backend webhook (server-to-server).
// Forwards the guest identity + chosen property.
module.exports = async function handler(req, res) {
  res.setHeader("Access-Control-Allow-Origin", "*");
  res.setHeader("Access-Control-Allow-Methods", "POST, OPTIONS");
  res.setHeader("Access-Control-Allow-Headers", "Content-Type");
  if (req.method === "OPTIONS") return res.status(200).end();

  try {
    const body = req.body || {};
    const baseUrl = String(process.env.BOOKING_BASE_URL || "").trim().replace(/\/+$/, "");
    const apiKey = String(process.env.X_TVE_API_KEY || "").trim();
    if (!baseUrl || !apiKey) return res.status(503).json({ status: "configuration_error", message: "Booking backend URL and webhook API key must be configured on the server." });
    const url = `${baseUrl}/thames-valley-estates/booking-guest`;

    const response = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json", "Accept": "application/json", "X-TVE-Auth": apiKey },
      body: JSON.stringify(body)
    });

    const responseText = await response.text();
    let data;
    try { data = JSON.parse(responseText); } catch { data = null; }

    res.status(response.status).json(data || { status: "error", message: responseText });
  } catch (err) {
    res.status(502).json({ status: "error", message: err.message || "Booking guest proxy failed." });
  }
};
