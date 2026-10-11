/**
 * TETRIS PRO - Modern Arcade Engine
 * 100% Standalone, Zero-Dependency, Web Audio Synthesizer, SRS Wall Kicks & Particle FX
 */

(function () {
    'use strict';

    // Game Constants
    const COLS = 10;
    const ROWS = 20;
    const BLOCK_SIZE = 30; // 300x600 canvas

    // Standard Tetromino Colors & Matrices
    const PIECES = {
        I: {
            color: '#00f0ff',
            shape: [
                [0, 0, 0, 0],
                [1, 1, 1, 1],
                [0, 0, 0, 0],
                [0, 0, 0, 0]
            ]
        },
        J: {
            color: '#0066ff',
            shape: [
                [1, 0, 0],
                [1, 1, 1],
                [0, 0, 0]
            ]
        },
        L: {
            color: '#ff7700',
            shape: [
                [0, 0, 1],
                [1, 1, 1],
                [0, 0, 0]
            ]
        },
        O: {
            color: '#ffe600',
            shape: [
                [1, 1],
                [1, 1]
            ]
        },
        S: {
            color: '#00ff66',
            shape: [
                [0, 1, 1],
                [1, 1, 0],
                [0, 0, 0]
            ]
        },
        T: {
            color: '#b000ff',
            shape: [
                [0, 1, 0],
                [1, 1, 1],
                [0, 0, 0]
            ]
        },
        Z: {
            color: '#ff0055',
            shape: [
                [1, 1, 0],
                [0, 1, 1],
                [0, 0, 0]
            ]
        }
    };

    // SRS Wall Kick Data
    const KICKS_JLSTZ = {
        '0>1': [[0, 0], [-1, 0], [-1, 1], [0, -2], [-1, -2]],
        '1>0': [[0, 0], [1, 0], [1, -1], [0, 2], [1, 2]],
        '1>2': [[0, 0], [1, 0], [1, -1], [0, 2], [1, 2]],
        '2>1': [[0, 0], [-1, 0], [-1, 1], [0, -2], [-1, -2]],
        '2>3': [[0, 0], [1, 0], [1, 1], [0, -2], [1, -2]],
        '3>2': [[0, 0], [-1, 0], [-1, -1], [0, 2], [-1, 2]],
        '3>0': [[0, 0], [-1, 0], [-1, -1], [0, 2], [-1, 2]],
        '0>3': [[0, 0], [1, 0], [1, 1], [0, -2], [1, -2]]
    };

    const KICKS_I = {
        '0>1': [[0, 0], [-2, 0], [1, 0], [-2, -1], [1, 2]],
        '1>0': [[0, 0], [2, 0], [-1, 0], [2, 1], [-1, -2]],
        '1>2': [[0, 0], [-1, 0], [2, 0], [-1, 2], [2, -1]],
        '2>1': [[0, 0], [1, 0], [-2, 0], [1, -2], [-2, 1]],
        '2>3': [[0, 0], [2, 0], [-1, 0], [2, 1], [-1, -2]],
        '3>2': [[0, 0], [-2, 0], [1, 0], [-2, -1], [1, 2]],
        '3>0': [[0, 0], [1, 0], [-2, 0], [1, -2], [-2, 1]],
        '0>3': [[0, 0], [-1, 0], [2, 0], [-1, 2], [2, -1]]
    };

    // Sound Synthesizer via Web Audio API
    class SoundEngine {
        constructor() {
            this.ctx = null;
            this.muted = false;
        }

        init() {
            if (!this.ctx) {
                const AudioCtx = window.AudioContext || window.webkitAudioContext;
                if (AudioCtx) this.ctx = new AudioCtx();
            }
            if (this.ctx && this.ctx.state === 'suspended') {
                this.ctx.resume();
            }
        }

        playTone(freq, type = 'sine', duration = 0.1, gainVal = 0.15) {
            if (this.muted || !this.ctx) return;
            try {
                const osc = this.ctx.createOscillator();
                const gain = this.ctx.createGain();
                osc.type = type;
                osc.frequency.setValueAtTime(freq, this.ctx.currentTime);
                gain.gain.setValueAtTime(gainVal, this.ctx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.001, this.ctx.currentTime + duration);
                osc.connect(gain);
                gain.connect(this.ctx.destination);
                osc.start();
                osc.stop(this.ctx.currentTime + duration);
            } catch (e) {}
        }

        move() { this.playTone(320, 'square', 0.05, 0.04); }
        rotate() { this.playTone(480, 'triangle', 0.08, 0.1); }
        drop() { this.playTone(180, 'triangle', 0.07, 0.12); }
        hardDrop() {
            if (this.muted || !this.ctx) return;
            try {
                const osc = this.ctx.createOscillator();
                const gain = this.ctx.createGain();
                osc.type = 'sawtooth';
                osc.frequency.setValueAtTime(140, this.ctx.currentTime);
                osc.frequency.exponentialRampToValueAtTime(40, this.ctx.currentTime + 0.15);
                gain.gain.setValueAtTime(0.2, this.ctx.currentTime);
                gain.gain.exponentialRampToValueAtTime(0.01, this.ctx.currentTime + 0.15);
                osc.connect(gain);
                gain.connect(this.ctx.destination);
                osc.start();
                osc.stop(this.ctx.currentTime + 0.15);
            } catch(e) {}
        }

        lineClear() {
            [440, 554, 659].forEach((f, i) => {
                setTimeout(() => this.playTone(f, 'sine', 0.15, 0.15), i * 60);
            });
        }

        tetrisFanfare() {
            [523, 659, 783, 1046].forEach((f, i) => {
                setTimeout(() => this.playTone(f, 'triangle', 0.25, 0.2), i * 90);
            });
        }

        levelUp() {
            [300, 400, 500, 700].forEach((f, i) => {
                setTimeout(() => this.playTone(f, 'sine', 0.18, 0.15), i * 80);
            });
        }

        gameOver() {
            [300, 260, 220, 180].forEach((f, i) => {
                setTimeout(() => this.playTone(f, 'sawtooth', 0.22, 0.15), i * 110);
            });
        }
    }

    // Particle FX Engine
    class Particle {
        constructor(x, y, color) {
            this.x = x;
            this.y = y;
            this.color = color;
            this.vx = (Math.random() - 0.5) * 8;
            this.vy = (Math.random() - 0.8) * 8;
            this.size = Math.random() * 4 + 2;
            this.alpha = 1;
            this.decay = Math.random() * 0.03 + 0.02;
        }

        update() {
            this.x += this.vx;
            this.y += this.vy;
            this.vy += 0.2; // gravity
            this.alpha -= this.decay;
        }

        draw(ctx) {
            ctx.save();
            ctx.globalAlpha = Math.max(0, this.alpha);
            ctx.fillStyle = this.color;
            ctx.shadowColor = this.color;
            ctx.shadowBlur = 8;
            ctx.beginPath();
            ctx.arc(this.x, this.y, this.size, 0, Math.PI * 2);
            ctx.fill();
            ctx.restore();
        }
    }

    // Core Tetris Game Engine
    class TetrisGame {
        constructor() {
            // DOM Elements
            this.canvas = document.getElementById('tetris-canvas');
            this.ctx = this.canvas.getContext('2d');
            this.holdCanvas = document.getElementById('hold-canvas');
            this.holdCtx = this.holdCanvas.getContext('2d');
            this.nextCanvases = [
                document.getElementById('next-1').getContext('2d'),
                document.getElementById('next-2').getContext('2d'),
                document.getElementById('next-3').getContext('2d')
            ];

            this.scoreEl = document.getElementById('score');
            this.highScoreEl = document.getElementById('high-score');
            this.levelEl = document.getElementById('level');
            this.linesEl = document.getElementById('lines');
            this.comboEl = document.getElementById('combo-count');
            this.comboBanner = document.getElementById('combo-popup');
            this.b2bBadge = document.getElementById('b2b-badge');

            this.gameOverOverlay = document.getElementById('game-over-overlay');
            this.pauseOverlay = document.getElementById('pause-overlay');
            this.controlsModal = document.getElementById('controls-modal');
            this.soundBtn = document.getElementById('sound-btn');

            this.sound = new SoundEngine();
            this.particles = [];

            // State
            this.grid = this.createGrid();
            this.bag = [];
            this.nextQueue = [];
            this.currentPiece = null;
            this.holdPieceType = null;
            this.canHold = true;

            this.score = 0;
            this.highScore = parseInt(localStorage.getItem('tetris_high_score') || '0', 10);
            this.level = 1;
            this.lines = 0;
            this.combo = -1;
            this.b2b = false;
            this.tetrisCount = 0;

            this.dropCounter = 0;
            this.dropInterval = 1000;
            this.lastTime = 0;
            this.lockTimer = 0;
            this.lockDelay = 500;
            this.isLocking = false;

            this.isRunning = false;
            this.isPaused = false;
            this.screenShake = 0;

            this.highScoreEl.textContent = this.highScore.toLocaleString();

            this.bindEvents();
            this.renderEmptyPreviews();
        }

        createGrid() {
            return Array.from({ length: ROWS }, () => Array(COLS).fill(0));
        }

        bindEvents() {
            // Keyboard Controls
            window.addEventListener('keydown', (e) => {
                if (['Space', 'ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight'].includes(e.code)) {
                    e.preventDefault();
                }

                if (!this.isRunning) {
                    if (e.code === 'Space' || e.code === 'Enter') this.start();
                    return;
                }

                if (e.code === 'KeyP' || e.code === 'Escape') {
                    this.togglePause();
                    return;
                }

                if (this.isPaused) return;

                switch (e.code) {
                    case 'ArrowLeft':
                    case 'KeyA':
                        this.move(-1);
                        break;
                    case 'ArrowRight':
                    case 'KeyD':
                        this.move(1);
                        break;
                    case 'ArrowDown':
                    case 'KeyS':
                        this.softDrop();
                        break;
                    case 'ArrowUp':
                    case 'KeyW':
                        this.rotate(1);
                        break;
                    case 'KeyZ':
                        this.rotate(-1);
                        break;
                    case 'Space':
                        this.hardDrop();
                        break;
                    case 'KeyC':
                    case 'ShiftLeft':
                    case 'ShiftRight':
                        this.hold();
                        break;
                    case 'KeyR':
                        this.start();
                        break;
                }
            });

            // UI Buttons
            document.getElementById('start-game-btn').addEventListener('click', () => {
                this.sound.init();
                this.start();
            });

            document.getElementById('pause-btn').addEventListener('click', () => this.togglePause());
            document.getElementById('resume-btn').addEventListener('click', () => this.togglePause());
            document.getElementById('restart-btn').addEventListener('click', () => {
                this.sound.init();
                this.start();
            });

            this.soundBtn.addEventListener('click', () => {
                this.sound.init();
                this.sound.muted = !this.sound.muted;
                this.soundBtn.innerHTML = this.sound.muted
                    ? '<i class="fa-solid fa-volume-xmark" style="color:#ff0055"></i>'
                    : '<i class="fa-solid fa-volume-high"></i>';
            });

            document.getElementById('help-btn').addEventListener('click', () => {
                this.controlsModal.classList.remove('hidden');
            });
            document.getElementById('close-help-btn').addEventListener('click', () => {
                this.controlsModal.classList.add('hidden');
            });

            // Touch Controls for Mobile/Tablet
            const bindTouch = (id, fn) => {
                const el = document.getElementById(id);
                if (!el) return;
                el.addEventListener('touchstart', (e) => {
                    e.preventDefault();
                    if (!this.isRunning) this.start();
                    else if (!this.isPaused) fn();
                });
                el.addEventListener('click', (e) => {
                    e.preventDefault();
                    if (!this.isRunning) this.start();
                    else if (!this.isPaused) fn();
                });
            };

            bindTouch('t-left', () => this.move(-1));
            bindTouch('t-right', () => this.move(1));
            bindTouch('t-down', () => this.softDrop());
            bindTouch('t-rot-cw', () => this.rotate(1));
            bindTouch('t-rot-ccw', () => this.rotate(-1));
            bindTouch('t-hard', () => this.hardDrop());
            bindTouch('t-hold', () => this.hold());
        }

        start() {
            this.grid = this.createGrid();
            this.bag = [];
            this.nextQueue = [];
            this.holdPieceType = null;
            this.canHold = true;

            this.score = 0;
            this.level = 1;
            this.lines = 0;
            this.combo = -1;
            this.b2b = false;
            this.tetrisCount = 0;
            this.dropInterval = 1000;
            this.particles = [];

            this.updateStatsUI();
            this.gameOverOverlay.classList.add('hidden');
            this.pauseOverlay.classList.add('hidden');

            for (let i = 0; i < 4; i++) {
                this.nextQueue.push(this.pullFromBag());
            }

            this.spawnPiece();
            this.isRunning = true;
            this.isPaused = false;
            this.lastTime = performance.now();

            requestAnimationFrame(this.gameLoop.bind(this));
        }

        pullFromBag() {
            if (this.bag.length === 0) {
                this.bag = Object.keys(PIECES);
                for (let i = this.bag.length - 1; i > 0; i--) {
                    const j = Math.floor(Math.random() * (i + 1));
                    [this.bag[i], this.bag[j]] = [this.bag[j], this.bag[i]];
                }
            }
            return this.bag.pop();
        }

        spawnPiece() {
            const type = this.nextQueue.shift();
            this.nextQueue.push(this.pullFromBag());

            const shape = PIECES[type].shape;
            this.currentPiece = {
                type: type,
                matrix: shape.map(row => [...row]),
                color: PIECES[type].color,
                x: Math.floor((COLS - shape[0].length) / 2),
                y: 0,
                rotation: 0
            };

            this.canHold = true;
            this.isLocking = false;
            this.lockTimer = 0;

            if (this.checkCollision(this.currentPiece.matrix, this.currentPiece.x, this.currentPiece.y)) {
                this.gameOver();
            }

            this.renderPreviews();
        }

        hold() {
            if (!this.canHold) return;
            this.sound.rotate();

            const curType = this.currentPiece.type;
            if (this.holdPieceType === null) {
                this.holdPieceType = curType;
                this.spawnPiece();
            } else {
                const swapType = this.holdPieceType;
                this.holdPieceType = curType;
                const shape = PIECES[swapType].shape;
                this.currentPiece = {
                    type: swapType,
                    matrix: shape.map(row => [...row]),
                    color: PIECES[swapType].color,
                    x: Math.floor((COLS - shape[0].length) / 2),
                    y: 0,
                    rotation: 0
                };
            }

            this.canHold = false;
            this.renderPreviews();
        }

        checkCollision(matrix, px, py) {
            for (let r = 0; r < matrix.length; r++) {
                for (let c = 0; c < matrix[r].length; c++) {
                    if (matrix[r][c]) {
                        const newX = px + c;
                        const newY = py + r;
                        if (newX < 0 || newX >= COLS || newY >= ROWS) return true;
                        if (newY >= 0 && this.grid[newY][newX]) return true;
                    }
                }
            }
            return false;
        }

        rotateMatrix(matrix, dir) {
            const N = matrix.length;
            const res = Array.from({ length: N }, () => Array(N).fill(0));
            for (let r = 0; r < N; r++) {
                for (let c = 0; c < N; c++) {
                    if (dir > 0) res[c][N - 1 - r] = matrix[r][c];
                    else res[N - 1 - c][r] = matrix[r][c];
                }
            }
            return res;
        }

        rotate(dir) {
            if (!this.currentPiece || this.currentPiece.type === 'O') return;

            const oldRot = this.currentPiece.rotation;
            const newRot = (oldRot + (dir > 0 ? 1 : 3)) % 4;
            const rotated = this.rotateMatrix(this.currentPiece.matrix, dir);

            const kickKey = `${oldRot}>${newRot}`;
            const kickTable = (this.currentPiece.type === 'I') ? KICKS_I : KICKS_JLSTZ;
            const kicks = kickTable[kickKey] || [[0, 0]];

            for (const [kx, ky] of kicks) {
                const targetX = this.currentPiece.x + kx;
                const targetY = this.currentPiece.y - ky; // inverted Y in standard matrix
                if (!this.checkCollision(rotated, targetX, targetY)) {
                    this.currentPiece.matrix = rotated;
                    this.currentPiece.x = targetX;
                    this.currentPiece.y = targetY;
                    this.currentPiece.rotation = newRot;
                    this.sound.rotate();
                    if (this.isLocking) this.lockTimer = 0; // reset lock delay on successful move
                    return;
                }
            }
        }

        move(dir) {
            if (!this.checkCollision(this.currentPiece.matrix, this.currentPiece.x + dir, this.currentPiece.y)) {
                this.currentPiece.x += dir;
                this.sound.move();
                if (this.isLocking) this.lockTimer = 0;
            }
        }

        softDrop() {
            if (!this.checkCollision(this.currentPiece.matrix, this.currentPiece.x, this.currentPiece.y + 1)) {
                this.currentPiece.y++;
                this.score += 1;
                this.updateStatsUI();
                this.sound.drop();
            } else {
                this.lockPiece();
            }
        }

        hardDrop() {
            let dropDist = 0;
            while (!this.checkCollision(this.currentPiece.matrix, this.currentPiece.x, this.currentPiece.y + 1)) {
                this.currentPiece.y++;
                dropDist++;
            }
            this.score += dropDist * 2;
            this.screenShake = 6;
            this.sound.hardDrop();
            this.spawnImpactParticles();
            this.lockPiece();
        }

        spawnImpactParticles() {
            const p = this.currentPiece;
            for (let r = 0; r < p.matrix.length; r++) {
                for (let c = 0; c < p.matrix[r].length; c++) {
                    if (p.matrix[r][c]) {
                        const px = (p.x + c) * BLOCK_SIZE + BLOCK_SIZE / 2;
                        const py = (p.y + r) * BLOCK_SIZE + BLOCK_SIZE;
                        for (let k = 0; k < 3; k++) {
                            this.particles.push(new Particle(px, py, p.color));
                        }
                    }
                }
            }
        }

        getGhostY() {
            let ghostY = this.currentPiece.y;
            while (!this.checkCollision(this.currentPiece.matrix, this.currentPiece.x, ghostY + 1)) {
                ghostY++;
            }
            return ghostY;
        }

        lockPiece() {
            const p = this.currentPiece;
            for (let r = 0; r < p.matrix.length; r++) {
                for (let c = 0; c < p.matrix[r].length; c++) {
                    if (p.matrix[r][c]) {
                        const gx = p.x + c;
                        const gy = p.y + r;
                        if (gy >= 0) {
                            this.grid[gy][gx] = p.color;
                        }
                    }
                }
            }

            this.clearLines();
            this.spawnPiece();
        }

        clearLines() {
            let cleared = 0;
            for (let r = ROWS - 1; r >= 0; r--) {
                if (this.grid[r].every(cell => cell !== 0)) {
                    cleared++;
                    // Spawn particles across cleared line
                    for (let c = 0; c < COLS; c++) {
                        const color = this.grid[r][c];
                        for (let k = 0; k < 4; k++) {
                            this.particles.push(new Particle(c * BLOCK_SIZE + 15, r * BLOCK_SIZE + 15, color));
                        }
                    }
                    this.grid.splice(r, 1);
                    this.grid.unshift(Array(COLS).fill(0));
                    r++; // check same row index again after shift
                }
            }

            if (cleared > 0) {
                this.lines += cleared;
                this.combo++;

                // Line Clear Scoring
                const basePoints = [0, 100, 300, 500, 800];
                let points = (basePoints[cleared] || 0) * this.level;

                if (cleared === 4) {
                    this.tetrisCount++;
                    if (this.b2b) {
                        points = Math.floor(points * 1.5);
                        this.showB2B();
                    }
                    this.b2b = true;
                    this.sound.tetrisFanfare();
                    this.showCombo(`TETRIS! +${points}`);
                } else {
                    this.b2b = false;
                    this.sound.lineClear();
                    if (this.combo > 0) {
                        const comboBonus = 50 * this.combo * this.level;
                        points += comboBonus;
                        this.showCombo(`COMBO ${this.combo}x! +${points}`);
                    }
                }

                this.score += points;

                // Level Progression
                const newLevel = Math.floor(this.lines / 10) + 1;
                if (newLevel > this.level) {
                    this.level = newLevel;
                    this.sound.levelUp();
                    this.dropInterval = Math.max(100, 1000 - (this.level - 1) * 85);
                }

                if (this.score > this.highScore) {
                    this.highScore = this.score;
                    localStorage.setItem('tetris_high_score', this.highScore);
                }

                this.updateStatsUI();
            } else {
                this.combo = -1;
                this.updateStatsUI();
            }
        }

        showCombo(text) {
            this.comboBanner.textContent = text;
            this.comboBanner.classList.add('show');
            setTimeout(() => this.comboBanner.classList.remove('show'), 1200);
        }

        showB2B() {
            this.b2bBadge.classList.add('show');
            setTimeout(() => this.b2bBadge.classList.remove('show'), 1200);
        }

        updateStatsUI() {
            this.scoreEl.textContent = this.score.toLocaleString();
            this.highScoreEl.textContent = this.highScore.toLocaleString();
            this.levelEl.textContent = this.level;
            this.linesEl.textContent = this.lines;
            this.comboEl.textContent = (this.combo > 0 ? `${this.combo}x` : '0x');
        }

        togglePause() {
            if (!this.isRunning) return;
            this.isPaused = !this.isPaused;
            if (this.isPaused) {
                this.pauseOverlay.classList.remove('hidden');
            } else {
                this.pauseOverlay.classList.add('hidden');
                this.lastTime = performance.now();
                requestAnimationFrame(this.gameLoop.bind(this));
            }
        }

        gameOver() {
            this.isRunning = false;
            this.sound.gameOver();

            document.getElementById('modal-title').textContent = 'انتهت اللعبة! GAME OVER';
            document.getElementById('modal-subtitle').textContent = 'أداء رائع! تفقد إحصائياتك:';
            document.getElementById('game-summary').style.display = 'flex';
            document.getElementById('final-score').textContent = this.score.toLocaleString();
            document.getElementById('final-level').textContent = this.level;
            document.getElementById('final-lines').textContent = this.lines;
            document.getElementById('final-tetris').textContent = this.tetrisCount;
            document.getElementById('start-game-btn').innerHTML = '<i class="fa-solid fa-rotate-right"></i> العب مرة أخرى';

            this.gameOverOverlay.classList.remove('hidden');
        }

        gameLoop(time = 0) {
            if (!this.isRunning || this.isPaused) return;

            const delta = time - this.lastTime;
            this.lastTime = time;

            this.dropCounter += delta;
            if (this.dropCounter > this.dropInterval) {
                if (!this.checkCollision(this.currentPiece.matrix, this.currentPiece.x, this.currentPiece.y + 1)) {
                    this.currentPiece.y++;
                    this.isLocking = false;
                } else {
                    this.isLocking = true;
                }
                this.dropCounter = 0;
            }

            if (this.isLocking) {
                this.lockTimer += delta;
                if (this.lockTimer >= this.lockDelay) {
                    this.lockPiece();
                }
            }

            this.updateParticles();
            this.draw();

            requestAnimationFrame(this.gameLoop.bind(this));
        }

        updateParticles() {
            for (let i = this.particles.length - 1; i >= 0; i--) {
                this.particles[i].update();
                if (this.particles[i].alpha <= 0) {
                    this.particles.splice(i, 1);
                }
            }
        }

        drawBlock(ctx, x, y, color, size, isGhost = false) {
            const px = x * size;
            const py = y * size;

            if (isGhost) {
                ctx.save();
                ctx.strokeStyle = color;
                ctx.lineWidth = 2;
                ctx.setLineDash([4, 3]);
                ctx.fillStyle = 'rgba(255, 255, 255, 0.04)';
                ctx.strokeRect(px + 2, py + 2, size - 4, size - 4);
                ctx.fillRect(px + 2, py + 2, size - 4, size - 4);
                ctx.restore();
                return;
            }

            ctx.save();
            // Main block fill
            ctx.fillStyle = color;
            ctx.shadowColor = color;
            ctx.shadowBlur = 6;
            ctx.fillRect(px + 1, py + 1, size - 2, size - 2);

            // 3D Bevel Highlight
            ctx.fillStyle = 'rgba(255, 255, 255, 0.35)';
            ctx.fillRect(px + 1, py + 1, size - 2, 3);
            ctx.fillRect(px + 1, py + 1, 3, size - 2);

            // 3D Bevel Shadow
            ctx.fillStyle = 'rgba(0, 0, 0, 0.35)';
            ctx.fillRect(px + size - 4, py + 1, 3, size - 2);
            ctx.fillRect(px + 1, py + size - 4, size - 2, 3);

            ctx.restore();
        }

        draw() {
            this.ctx.save();

            // Screen shake
            if (this.screenShake > 0) {
                const sx = (Math.random() - 0.5) * this.screenShake;
                const sy = (Math.random() - 0.5) * this.screenShake;
                this.ctx.translate(sx, sy);
                this.screenShake *= 0.8;
                if (this.screenShake < 0.5) this.screenShake = 0;
            }

            // Clear Canvas & draw Grid lines
            this.ctx.fillStyle = '#05070c';
            this.ctx.fillRect(0, 0, this.canvas.width, this.canvas.height);

            this.ctx.strokeStyle = 'rgba(255, 255, 255, 0.03)';
            this.ctx.lineWidth = 1;
            for (let r = 0; r < ROWS; r++) {
                this.ctx.beginPath();
                this.ctx.moveTo(0, r * BLOCK_SIZE);
                this.ctx.lineTo(this.canvas.width, r * BLOCK_SIZE);
                this.ctx.stroke();
            }
            for (let c = 0; c < COLS; c++) {
                this.ctx.beginPath();
                this.ctx.moveTo(c * BLOCK_SIZE, 0);
                this.ctx.lineTo(c * BLOCK_SIZE, this.canvas.height);
                this.ctx.stroke();
            }

            // Draw Locked Grid Blocks
            for (let r = 0; r < ROWS; r++) {
                for (let c = 0; c < COLS; c++) {
                    if (this.grid[r][c]) {
                        this.drawBlock(this.ctx, c, r, this.grid[r][c], BLOCK_SIZE);
                    }
                }
            }

            // Draw Ghost Piece
            if (this.currentPiece) {
                const ghostY = this.getGhostY();
                const m = this.currentPiece.matrix;
                for (let r = 0; r < m.length; r++) {
                    for (let c = 0; c < m[r].length; c++) {
                        if (m[r][c]) {
                            this.drawBlock(this.ctx, this.currentPiece.x + c, ghostY + r, this.currentPiece.color, BLOCK_SIZE, true);
                        }
                    }
                }
            }

            // Draw Falling Active Piece
            if (this.currentPiece) {
                const m = this.currentPiece.matrix;
                for (let r = 0; r < m.length; r++) {
                    for (let c = 0; c < m[r].length; c++) {
                        if (m[r][c]) {
                            this.drawBlock(this.ctx, this.currentPiece.x + c, this.currentPiece.y + r, this.currentPiece.color, BLOCK_SIZE);
                        }
                    }
                }
            }

            // Draw Explosive Particles
            for (const p of this.particles) {
                p.draw(this.ctx);
            }

            this.ctx.restore();
        }

        drawMiniPiece(ctx, type, size = 20) {
            ctx.clearRect(0, 0, ctx.canvas.width, ctx.canvas.height);
            if (!type) return;

            const piece = PIECES[type];
            const matrix = piece.shape;
            const w = matrix[0].length * size;
            const h = matrix.length * size;
            const offsetX = (ctx.canvas.width - w) / 2;
            const offsetY = (ctx.canvas.height - h) / 2;

            for (let r = 0; r < matrix.length; r++) {
                for (let c = 0; c < matrix[r].length; c++) {
                    if (matrix[r][c]) {
                        this.drawBlock(ctx, c + offsetX / size, r + offsetY / size, piece.color, size);
                    }
                }
            }
        }

        renderPreviews() {
            // Hold Canvas
            this.drawMiniPiece(this.holdCtx, this.holdPieceType, 22);

            // Next Queue Canvases
            for (let i = 0; i < 3; i++) {
                this.drawMiniPiece(this.nextCanvases[i], this.nextQueue[i], 18);
            }
        }

        renderEmptyPreviews() {
            this.holdCtx.clearRect(0, 0, this.holdCanvas.width, this.holdCanvas.height);
            for (let i = 0; i < 3; i++) {
                this.nextCanvases[i].clearRect(0, 0, 90, 90);
            }
        }
    }

    // Initialize Game when DOM is loaded
    document.addEventListener('DOMContentLoaded', () => {
        window.tetrisGame = new TetrisGame();
    });
})();