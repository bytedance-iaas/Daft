// npm run check:api — fail when src/api/schema.d.ts no longer matches docs/contracts/openapi.yaml.
import { generate, readCommitted } from './gen-api-lib.mjs';

const fresh = await generate();
if (fresh !== readCommitted()) {
  console.error('src/api/schema.d.ts is stale: the contract changed. Run `npm run gen:api` and commit the result.');
  process.exit(1);
}
console.log('src/api/schema.d.ts matches the contract');
