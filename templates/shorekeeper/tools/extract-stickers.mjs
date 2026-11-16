import {createRequire} from 'node:module';
import {readdir, mkdir, writeFile, stat} from 'node:fs/promises';
import {createReadStream} from 'node:fs';
import {createHash} from 'node:crypto';
import path from 'node:path';
import {fileURLToPath} from 'node:url';

const root = fileURLToPath(new URL('../', import.meta.url));

const require = createRequire(import.meta.url);
const sharp = require(process.env.PLAYWRIGHT_MODULE_ROOT ? path.join(process.env.PLAYWRIGHT_MODULE_ROOT, 'sharp') : 'sharp');
const source = path.resolve(process.argv[2] || path.join(root, '../../..', 'Q版表情包'));
const output = path.join(root, 'assets/emotes');
await mkdir(output, {recursive:true});
const files = (await readdir(source)).filter(name => /\.webp$/i.test(name)).sort();
const manifest = {source:'用户提供的 Q版表情包 文件夹', frame:0, entries:[]};
for (const filename of files) {
  const input = path.join(source, filename);
  const id = filename.match(/^\d+/)?.[0];
  if (!id) throw new Error('Missing numeric sticker ID: ' + filename);
  const hash = createHash('sha256');
  for await (const chunk of createReadStream(input)) hash.update(chunk);
  const before = await sharp(input, {page:0, pages:1}).metadata();
  const destination = path.join(output, id + '.png');
  // Decode exactly frame zero. Do not open all frames, animate, crop or recolor.
  await sharp(input, {page:0, pages:1, animated:false}).png().toFile(destination);
  const after = await sharp(destination).metadata();
  if ((after.pages || 1) !== 1 || after.width !== before.width || after.height !== before.height) throw new Error('Unexpected output dimensions or animation');
  const firstPixels = await sharp(input, {page:0, pages:1, animated:false}).ensureAlpha().raw().toBuffer();
  const outputPixels = await sharp(destination).ensureAlpha().raw().toBuffer();
  if (!firstPixels.equals(outputPixels)) throw new Error('First-frame pixels changed: ' + filename);
  manifest.entries.push({id, sourceFile:filename, sourceSHA256:hash.digest('hex'), sourceFrames:before.pages || 1,
    file:id+'.png', width:after.width, height:after.height, alpha:after.hasAlpha,
    sourceBytes:(await stat(input)).size, bytes:(await stat(destination)).size, firstFramePixelsVerified:true});
}
await writeFile(path.join(output, 'manifest.json'), JSON.stringify(manifest,null,2)+'\n');
console.log(JSON.stringify({files:files.length, sourceBytes:manifest.entries.reduce((n,e)=>n+e.sourceBytes,0), pngBytes:manifest.entries.reduce((n,e)=>n+e.bytes,0), framesPerOutput:1}));
