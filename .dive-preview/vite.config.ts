import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import path from 'path';

export default defineConfig({
	plugins: [react()],
	// MotherDuck's WASM client needs SharedArrayBuffer, which requires cross-origin isolation.
	server: {
		headers: {
			'Cross-Origin-Opener-Policy': 'same-origin',
			'Cross-Origin-Embedder-Policy': 'require-corp',
		},
	},
	resolve: {
		alias: {
			'@motherduck/react-sql-query': path.resolve(__dirname, 'src/md-sdk.tsx'),
		},
	},
});
