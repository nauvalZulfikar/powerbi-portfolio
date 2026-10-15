// Parse every .pq file with Microsoft's Power Query parser: ok / FAIL file:line:col message.
//   node check.mjs ../../powerquery
import pq from "@microsoft/powerquery-parser";
import * as fs from "node:fs";
import * as path from "node:path";

const dir = process.argv[2];
const files = fs.readdirSync(dir).filter((f) => f.endsWith(".pq")).sort();
let errors = 0;
for (const file of files) {
  const r = await pq.TaskUtils.tryLexParse(pq.DefaultSettings, fs.readFileSync(path.join(dir, file), "utf8"));
  if (pq.TaskUtils.isParseStageOk(r)) {
    console.log(`ok  ${path.basename(file, ".pq")}`);
  } else {
    errors++;
    const pos = r.error.innerError?.foundToken?.token?.positionStart ?? r.error.innerError?.positionStart;
    console.log(`FAIL ${file}:${pos ? pos.lineNumber + 1 : 0}:${pos ? pos.lineCodeUnit + 1 : 0} ${r.error.message}`);
  }
}
console.log(`${files.length} queries, ${errors} errors`);
process.exit(errors ? 1 : 0);
