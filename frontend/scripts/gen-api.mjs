// npm run gen:api — regenerate src/api/schema.d.ts from the C4 contract.
import { writeFileSync } from 'node:fs';
import { OUTPUT, generate } from './gen-api-lib.mjs';

const text = await generate();
writeFileSync(OUTPUT, text);
console.log(`wrote ${OUTPUT}`);
