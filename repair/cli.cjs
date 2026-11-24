const fs = require('node:fs');
const repair = require('./mediaRepairCjs.cjs');
const [kind, input, output, referencePath] = process.argv.slice(2);
try {
  const reference = referencePath ? fs.readFileSync(referencePath) : null;
  const handlers = {
    jpeg: repair.repairJpegBufferCjs,
    png: repair.repairPngBufferCjs,
    avi: repair.repairAviBufferCjs,
  };
  let result;
  if (kind === 'mp4') {
    result = repair.repairMp4StreamingFileCjs(input, output, reference);
  } else {
    result = handlers[kind](fs.readFileSync(input), reference);
    if (result?.data) fs.writeFileSync(output, result.data);
    result = { ok: Boolean(result?.data), note: result?.note };
  }
  console.log(JSON.stringify(result || { ok: false }));
} catch (err) {
  console.log(JSON.stringify({ ok: false, note: err.message }));
}
