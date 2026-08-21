import { createReadStream } from "node:fs";
import { stat } from "node:fs/promises";
import { createServer } from "node:http";
import { extname, join, normalize, resolve } from "node:path";

const root = resolve(process.cwd());
const port = Number(process.env.PORT || 4187);
// .mjs must be a JavaScript MIME type: module scripts enforce strict MIME
// checking, so serving it as octet-stream silently kills the whole page.
// GitHub Pages already maps .mjs correctly; this map is for local preview.
const mimeTypes = { ".css": "text/css; charset=utf-8", ".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8", ".mjs": "text/javascript; charset=utf-8", ".json": "application/json; charset=utf-8", ".svg": "image/svg+xml", ".mp3": "audio/mpeg" };
const server = createServer(async (request, response) => {
  const rawPath = decodeURIComponent(new URL(request.url || "/", "http://localhost").pathname);
  const requested = rawPath === "/" ? "/index.html" : rawPath;
  const filePath = resolve(join(root, normalize(requested)));
  if (!filePath.startsWith(root)) { response.writeHead(403).end("Forbidden"); return; }
  try {
    const fileStat = await stat(filePath);
    if (!fileStat.isFile()) throw new Error("Not a file");
    response.writeHead(200, { "Content-Type": mimeTypes[extname(filePath)] || "application/octet-stream", "Cache-Control": "no-store" });
    createReadStream(filePath).pipe(response);
  } catch {
    response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" }); response.end("Not found");
  }
});
server.listen(port, "127.0.0.1", () => console.log(`Local URL: http://127.0.0.1:${port}/`));
