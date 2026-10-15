// The schema as the Python server loads it.
import {schema} from './app/schema.ts';

process.stdout.write(JSON.stringify(schema));
