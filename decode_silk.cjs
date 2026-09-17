// Local-only adapter for the existing LLBot silk-wasm package.
const fs = require('fs');
(async () => {
  const [modulePath, inputPath, outputPath] = process.argv.slice(2);
  const silk = require(modulePath);
  const input = fs.readFileSync(inputPath);
  if (!input.length || input.length > 8 * 1024 * 1024 || !silk.isSilk(input)) throw new Error('invalid input');
  const result = await silk.decode(input, 24000);
  if (!result.data.length || result.data.length > 32 * 1024 * 1024 - 44) throw new Error('output limit');
  fs.writeFileSync(outputPath, result.data);
})().catch(() => { process.stderr.write('Silk decoding failed\n'); process.exitCode = 1; });
