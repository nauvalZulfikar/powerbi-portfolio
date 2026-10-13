// Parse every Power Query (M) expression in a TMDL folder with Microsoft's own M parser
// (@microsoft/powerquery-parser, the one behind the Power Query VS Code extension). Syntax only:
// it does not run the query or check that files, columns or the SQL endpoint exist.
//   node tools/m-validate/validate.mjs powerbi/SalesPipeline.SemanticModel/definition
import { readFileSync, readdirSync, statSync } from "node:fs";
import { join } from "node:path";
import { DefaultSettings, TaskUtils } from "@microsoft/powerquery-parser";

const files = (dir) => readdirSync(dir).flatMap((f) => {
  const p = join(dir, f);
  return statSync(p).isDirectory() ? files(p) : p.endsWith(".tmdl") ? [p] : [];
});
const indent = (l) => l.match(/^\t*/)[0].length;
const blocks = [];
for (const file of files(process.argv[2])) {
  const lines = readFileSync(file, "utf8").split("\n");
  let skipDax = false;
  lines.forEach((line, i) => {
    if (/^\s*partition .* = calculated/.test(line)) skipDax = true;
    else if (/^\s*partition .* = m\s*$/.test(line)) skipDax = false;
    const one = line.match(/^expression (\S+) = (.+?) meta \[/);
    if (one) return blocks.push({ name: `${file}:${i + 1} ${one[1]}`, text: one[2] });
    const head = line.match(/^(\s*)(source|expression \S+|pollingExpression|sourceExpression) =\s*$/);
    if (!head || (head[2] === "source" && skipDax)) return;
    const min = indent(line) + 2, body = [];
    for (const l of lines.slice(i + 1)) {
      if (l.trim() && indent(l) < min) break;
      body.push(l);
    }
    blocks.push({ name: `${file}:${i + 1} ${head[2]}`, text: body.join("\n") });
  });
}
let bad = 0;
for (const b of blocks) {
  const r = await TaskUtils.tryLexParse(DefaultSettings, b.text);
  if (!TaskUtils.isParseStageOk(r)) {
    bad++;
    console.error(`✗ ${b.name}: ${r.error?.message ?? "lex/parse error"}`);
  }
}
console.log(`${bad ? "✗" : "✓"} M parser: ${blocks.length - bad}/${blocks.length} Power Query expressions parse`);
process.exit(bad ? 1 : 0);
