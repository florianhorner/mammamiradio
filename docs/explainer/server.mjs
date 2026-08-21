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
  let rawPath;
  try {
    rawPath = decodeURIComponent(new URL(request.url || "/", "http://localhost").pathname);
  } catch {
    // Malformed percent-encoding used to throw outside any handler and hang
    // the socket with no response at all.
    response.writeHead(400, { "Content-Type": "text/plain; charset=utf-8" }).end("Bad request");
    return;
  }
  const requested = rawPath === "/" ? "/index.html" : rawPath;
  const filePath = resolve(join(root, normalize(requested)));
  if (!filePath.startsWith(root)) { response.writeHead(403).end("Forbidden"); return; }
  try {
    const fileStat = await stat(filePath);
    if (!fileStat.isFile()) throw new Error("Not a file");
    const contentType = mimeTypes[extname(filePath)] || "application/octet-stream";
    // Range support matters: without it Chromium marks the audio unseekable
    // and snaps every currentTime assignment back to 0 — the local preview
    // would behave differently from GitHub Pages, which serves Ranges.
    const range = /^bytes=(\d*)-(\d*)$/.exec(request.headers.range || "");
    if (range && (range[1] || range[2])) {
      const start = range[1] ? Number(range[1]) : Math.max(fileStat.size - Number(range[2]), 0);
      const end = range[1] && range[2] ? Math.min(Number(range[2]), fileStat.size - 1) : fileStat.size - 1;
      if (start > end || start >= fileStat.size) {
        response.writeHead(416, { "Content-Range": `bytes */${fileStat.size}` }).end();
        return;
      }
      response.writeHead(206, {
        "Content-Type": contentType,
        "Content-Range": `bytes ${start}-${end}/${fileStat.size}`,
        "Content-Length": end - start + 1,
        "Accept-Ranges": "bytes",
        "Cache-Control": "no-store",
      });
      createReadStream(filePath, { start, end }).pipe(response);
      return;
    }
    response.writeHead(200, { "Content-Type": contentType, "Content-Length": fileStat.size, "Accept-Ranges": "bytes", "Cache-Control": "no-store" });
    createReadStream(filePath).pipe(response);
  } catch {
    response.writeHead(404, { "Content-Type": "text/plain; charset=utf-8" }); response.end("Not found");
  }
});
server.listen(port, "127.0.0.1", () => console.log(`Local URL: http://127.0.0.1:${port}/`));
