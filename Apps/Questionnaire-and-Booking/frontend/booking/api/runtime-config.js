module.exports = function handler(_req, res) {
  var questionnaireSubmitBaseUrl = String(process.env.QUESTIONNAIRE_SUBMIT_BASE_URL || "").trim().replace(/\/+$/, "");
  var bookingBaseUrl = String(process.env.BOOKING_BASE_URL || process.env.BOOKING_PRIMARY_URL || "").trim().replace(/\/+$/, "");

  res.setHeader("Content-Type", "application/json; charset=utf-8");
  res.setHeader("Cache-Control", "no-store, max-age=0");
  res.setHeader("Access-Control-Allow-Origin", "*");

  var config = {};
  if (questionnaireSubmitBaseUrl) config.questionnaireSubmitBaseUrl = questionnaireSubmitBaseUrl;
  if (bookingBaseUrl) {
    config.bookingBaseUrl = bookingBaseUrl;
    config.bookingPrimaryUrl = bookingBaseUrl;
    config.bookingFallbackUrl = bookingBaseUrl;
  }
  res.status(200).json(config);
};
