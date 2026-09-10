// ==========================================
// DENTALINK AUDIO NOTIFICATION ENGINE
// ==========================================
const AudioEngine = {
    ctx: null,

    init() {
        if (!this.ctx) {
            const AudioContext = window.AudioContext || window.webkitAudioContext;
            this.ctx = new AudioContext();
        }
        if (this.ctx.state === 'suspended') {
            this.ctx.resume();
        }
    },

    playTone(toneType) {
        this.init();
        const now = this.ctx.currentTime;
        const osc = this.ctx.createOscillator();
        const gain = this.ctx.createGain();

        osc.connect(gain);
        gain.connect(this.ctx.destination);

        switch (toneType) {
            case 'chime':
                // Two-tone soft bell (880Hz -> 1320Hz)
                osc.type = 'sine';
                osc.frequency.setValueAtTime(880, now);
                osc.frequency.exponentialRampToValueAtTime(1320, now + 0.12);
                gain.gain.setValueAtTime(0.25, now);
                gain.gain.exponentialRampToValueAtTime(0.001, now + 0.5);
                osc.start(now);
                osc.stop(now + 0.5);
                break;

            case 'ping':
                // High-frequency subtle ping (1760Hz)
                osc.type = 'triangle';
                osc.frequency.setValueAtTime(1760, now);
                gain.gain.setValueAtTime(0.2, now);
                gain.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
                osc.start(now);
                osc.stop(now + 0.35);
                break;

            case 'pop':
                // Modern, punchy UI bubble pop
                osc.type = 'sine';
                osc.frequency.setValueAtTime(400, now);
                osc.frequency.exponentialRampToValueAtTime(800, now + 0.08);
                gain.gain.setValueAtTime(0.3, now);
                gain.gain.exponentialRampToValueAtTime(0.001, now + 0.18);
                osc.start(now);
                osc.stop(now + 0.18);
                break;

            case 'alert':
                // Dual alert pulse (two short beeps)
                osc.type = 'sine';
                osc.frequency.setValueAtTime(950, now);
                gain.gain.setValueAtTime(0.25, now);
                gain.gain.setValueAtTime(0.01, now + 0.08);
                gain.gain.setValueAtTime(0.25, now + 0.12);
                gain.gain.exponentialRampToValueAtTime(0.001, now + 0.35);
                osc.start(now);
                osc.stop(now + 0.35);
                break;

            default:
                // Default chime
                this.playTone('chime');
        }
    }
};

// Play alert based on user preferences
function triggerNotificationSound() {
    const soundEnabled = localStorage.getItem('dl_sound_enabled') !== 'false';
    if (!soundEnabled) return;

    const selectedTone = localStorage.getItem('dl_sound_tone') || 'chime';
    AudioEngine.playTone(selectedTone);
}