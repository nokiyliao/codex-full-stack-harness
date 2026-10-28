// Minimal stand-in for a supervised switchboard worker: ready message, drain, in-flight tracking.
import http from "node:http";

const active = new Set();
const server = http.createServer((request, response) => {
  active.add(response);
  response.once("close", () => active.delete(response));
  const reply = () => { response.setHeader("connection", "close"); response.end(String(process.pid)); };
  if (request.url === "/slow") setTimeout(reply, 1500);
  else reply();
});
server.listen(Number(process.env.SUPERVISOR_FIXTURE_PORT), "127.0.0.1", () => {
  process.send({ type: "switchboard-ready", adapter_source_sha256: "fixture" });
});
process.on("message", (message) => {
  if (message?.type !== "switchboard-drain") return;
  server.close();
  const finish = () => (active.size ? setTimeout(finish, 20) : process.exit(0));
  finish();
});
