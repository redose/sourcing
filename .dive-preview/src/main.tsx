import { createRoot } from 'react-dom/client';
import { useState } from 'react';
import { MotherDuckSDKProvider, useConnectionStatus } from './md-sdk';
import { AlertCircle } from 'lucide-react';
import Dive, { StartScreen } from './dive';

// ?intro shows just the intro, on a loop you can replay by clicking, to look at it properly.
function IntroOnly() {
	const [run, setRun] = useState(0);
	return (
		<div onClick={() => setRun(run + 1)} style={{ cursor: 'pointer' }} title="Click to replay">
			<StartScreen key={run} />
		</div>
	);
}

function ConnectionGate({ children }: { children: React.ReactNode }) {
	const { isConnected, isConnecting, error } = useConnectionStatus();
	// The Dive shows the start screen itself until its first data arrives (connecting included), so render it straight
	// away: one start screen from first paint to first data, instead of two in a row (which made Spunion jump twice).
	if (error)
		return (
			<div className="flex items-center justify-center h-screen gap-2 text-red-600">
				<AlertCircle size={20} />
				Connection failed: {error.message}
			</div>
		);
	if (!isConnected && !isConnecting) return <StartScreen />;
	return <>{children}</>;
}

// The static build (VITE_STATIC=1) needs no token: it reads exported files in the browser.
const token = import.meta.env.VITE_STATIC === '1' ? 'static' : import.meta.env.VITE_MOTHERDUCK_TOKEN;
if (!token) {
	document.getElementById('root')!.innerHTML =
		'<p style="padding:2rem;color:red">Missing VITE_MOTHERDUCK_TOKEN in .env</p>';
} else {
	createRoot(document.getElementById('root')!).render(
		new URLSearchParams(window.location.search).has('intro') ? (
			<IntroOnly />
		) : (
		<MotherDuckSDKProvider token={token}>
			<ConnectionGate>
				<Dive />
			</ConnectionGate>
		</MotherDuckSDKProvider>
		),
	);
}
