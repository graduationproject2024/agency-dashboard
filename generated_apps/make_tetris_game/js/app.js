export const COLORS = [
    null,
    '#E11D48', // 1: T (Rose Red)
    '#0284C7', // 2: O (Sky Blue)
    '#10B981', // 3: L (Emerald Green)
    '#D946EF', // 4: J (Fuchsia)
    '#F97316', // 5: I (Warm Orange)
    '#EAB308', // 6: S (Golden Yellow)
    '#6366F1', // 7: Z (Indigo)
];

export function createMatrix(type) {
    if (type === 'T') {
        return [
            [0, 1, 0],
            [1, 1, 1],
            [0, 0, 0],
        ];
    } else if (type === 'O') {
        return [
            [2, 2],
            [2, 2],
        ];
    } else if (type === 'L') {
        return [
            [0, 0, 3],
            [3, 3, 3],
            [0, 0, 0],
        ];
    } else if (type === 'J') {
        return [
            [4, 0, 0],
            [4, 4, 4],
            [0, 0, 0],
        ];
    } else if (type === 'I') {
        return [
            [0, 5, 0, 0],
            [0, 5, 0, 0],
            [0, 5, 0, 0],
            [0, 5, 0, 0],
        ];
    } else if (type === 'S') {
        return [
            [0, 6, 6],
            [6, 6, 0],
            [0, 0, 0],
        ];
    } else if (type === 'Z') {
        return [
            [7, 7, 0],
            [0, 7, 7],
            [0, 0, 0],
        ];
    }
}

export function createMatrixDim(w, h) {
    const matrix = [];
    while (h--) {
        matrix.push(new Array(w).fill(0));
    }
    return matrix;
}

import { COLORS } from './constants.js';

export class GameRenderer {
    constructor(canvasElement) {
        this.canvas = canvasElement;
        this.context = this.canvas.getContext('2d');
        this.context.scale(20, 20);
    }

    draw(arena, player) {
        // رسم خلفية لوحة اللعب
        const isDark = document.documentElement.classList.contains('dark');
        this.context.fillStyle = isDark ? '#070A12' : '#F1F5F9';
        this.context.fillRect(0, 0, this.canvas.width, this.canvas.height);

        this.drawMatrix(arena, {x: 0, y: 0});
        this.drawMatrix(player.matrix, player.pos);
    }

    drawMatrix(matrix, offset) {
        matrix.forEach((row, y) => {
            row.forEach((value, x) => {
                if (value !== 0) {
                    this.context.fillStyle = COLORS[value];
                    this.context.fillRect(x + offset.x, y + offset.y, 1, 1);
                    
                    // تأثير إطار داخلي خفيف لتعزيز الفخامة البصرية
                    this.context.lineWidth = 0.05;
                    this.context.strokeStyle = 'rgba(255, 255, 255, 0.25)';
                    this.context.strokeRect(x + offset.x, y + offset.y, 1, 1);
                }
            });
        });
    }
}

import { createMatrix, createMatrixDim } from './constants.js';

export class GameManager {
    constructor() {
        this.arena = createMatrixDim(12, 20);
        this.player = {
            pos: {x: 0, y: 0},
            matrix: null,
            score: 0,
        };
        this.dropCounter = 0;
        this.dropInterval = 1000;
        this.lastTime = 0;
    }

    reset() {
        this.arena.forEach(row => row.fill(0));
        this.player.score = 0;
        this.playerReset();
    }

    playerReset() {
        const pieces = 'TJLOSZI';
        this.player.matrix = createMatrix(pieces[(pieces.length * Math.random()) | 0]);
        this.player.pos.y = 0;
        this.player.pos.x = (this.arena[0].length / 2 | 0) - (this.player.matrix[0].length / 2 | 0);
        
        if (this.collide()) {
            this.arena.forEach(row => row.fill(0));
            this.player.score = 0;
        }
    }

    collide() {
        const [m, o] = [this.player.matrix, this.player.pos];
        for (let y = 0; y < m.length; ++y) {
            for (let x = 0; x < m[y].length; ++x) {
                if (m[y][x] !== 0 &&
                   (this.arena[y + o.y] && this.arena[y + o.y][x + o.x]) !== 0) {
                    return true;
                }
            }
        }
        return false;
    }

    merge() {
        this.player.matrix.forEach((row, y) => {
            row.forEach((value, x) => {
                if (value !== 0) {
                    this.arena[y + this.player.pos.y][x + this.player.pos.x] = value;
                }
            });
        });
    }

    arenaSweep() {
        let rowCount = 1;
        outer: for (let y = this.arena.length - 1; y > 0; --y) {
            for (let x = 0; x < this.arena[y].length; ++x) {
                if (this.arena[y][x] === 0) {
                    continue outer;
                }
            }
            const row = this.arena.splice(y, 1)[0].fill(0);
            this.arena.unshift(row);
            ++y;

            this.player.score += rowCount * 10;
            rowCount *= 2;
        }
    }

    playerDrop() {
        this.player.pos.y++;
        if (this.collide()) {
            this.player.pos.y--;
            this.merge();
            this.playerReset();
            this.arenaSweep();
        }
        this.dropCounter = 0;
    }

    playerMove(dir) {
        this.player.pos.x += dir;
        if (this.collide()) {
            this.player.pos.x -= dir;
        }
    }

    playerRotate() {
        const x = this.player.pos.x;
        let offset = 1;
        this.rotateMatrix(this.player.matrix);
        while (this.collide()) {
            this.player.pos.x += offset;
            offset = -(offset + (offset > 0 ? 1 : -1));
            if (offset > this.player.matrix[0].length) {
                this.rotateMatrix(this.player.matrix);
                this.player.pos.x = x;
                return;
            }
        }
    }

    rotateMatrix(matrix) {
        for (let y = 0; y < matrix.length; ++y) {
            for (let x = 0; x < y; ++x) {
                [matrix[x][y], matrix[y][x]] = [matrix[y][x], matrix[x][y]];
            }
        }
        matrix.forEach(row => row.reverse());
    }
}

import { GameManager } from './game.js';
import { GameRenderer } from './canvas.js';

const canvas = document.getElementById('tetris');
const scoreElement = document.getElementById('score');
const startBtn = document.getElementById('start-btn');
const themeToggle = document.getElementById('theme-toggle');
const toast = document.getElementById('toast');
const toastMessage = document.getElementById('toast-message');

const game = new GameManager();
const renderer = new GameRenderer(canvas);

let animationId = null;

// نظام الإشعارات العائمة (Toast Notifications)
function showToast(message) {
    toastMessage.innerText = message;
    toast.classList.remove('hidden');
    setTimeout(() => {
        toast.classList.add('hidden');
    }, 3000);
}

// محرك الحلقة الرئيسية للعبة (Game Loop)
function update(time = 0) {
    const deltaTime = time - game.lastTime;
    game.lastTime = time;
    game.dropCounter += deltaTime;
    
    if (game.dropCounter > game.dropInterval) {
        game.playerDrop();
    }
    
    renderer.draw(game.arena, game.player);
    scoreElement.innerText = game.player.score;
    
    animationId = requestAnimationFrame(update);
}

// مستمعي أحداث لوحة المفاتيح
document.addEventListener('keydown', event => {
    if (event.key === 'ArrowLeft') {
        game.playerMove(-1);
    } else if (event.key === 'ArrowRight') {
        game.playerMove(1);
    } else if (event.key === 'ArrowDown') {
        game.playerDrop();
    } else if (event.key === 'ArrowUp') {
        game.playerRotate();
    }
});

// بدء اللعبة
startBtn.addEventListener('click', () => {
    game.reset();
    game.lastTime = performance.now();
    if (animationId) {
        cancelAnimationFrame(animationId);
    }
    update();
    showToast('✨ انطلقت لعبة Block Drop بنجاح!');
    startBtn.blur();
});

// تبديل الثيم (Light / Dark Theme Toggle)
themeToggle.addEventListener('click', () => {
    const htmlEl = document.documentElement;
    if (htmlEl.classList.contains('dark')) {
        htmlEl.classList.remove('dark');
        showToast('☀️ تم تفعيل الثيم النهاري الفاخر');
    } else {
        htmlEl.classList.add('dark');
        showToast('🌙 تم تفعيل الثيم الداكن الفاخر');
    }
});

// تفعيل الأسئلة الشائعة (FAQ Accordion)
document.querySelectorAll('.faq-question').forEach(button => {
    button.addEventListener('click', () => {
        const item = button.parentElement;
        const isActive = item.classList.contains('active');
        
        document.querySelectorAll('.faq-item').forEach(faq => faq.classList.remove('active'));
        if (!isActive) {
            item.classList.add('active');
        }
    });
});

// رسم اللوحة مبدئياً عند التحميل
renderer.draw(game.arena, game.player);

/* Animation & Interaction Enhancements */
import { GameManager } from './game.js';
import { GameRenderer } from './canvas.js';

const canvas = document.getElementById('tetris');
const scoreElement = document.getElementById('score');
const startBtn = document.getElementById('start-btn');
const themeToggle = document.getElementById('theme-toggle');
const toast = document.getElementById('toast');
const toastMessage = document.getElementById('toast-message');

const game = new GameManager();
const renderer = new GameRenderer(canvas);

let animationId = null;
let lastScore = 0;

// نظام الإشعارات العائمة الاحترافي مع دعم الطابور التراكمي
function showToast(message) {
    toastMessage.innerText = message;
    toast.classList.remove('hidden');
    
    if (window.toastTimeout) clearTimeout(window.toastTimeout);
    
    window.toastTimeout = setTimeout(() => {
        toast.classList.add('hidden');
    }, 3500);
}

// محرك الحلقة الرئيسية للعبة (Game Loop محسّن لـ 60 FPS)
function update(time = 0) {
    const deltaTime = time - game.lastTime;
    game.lastTime = time;
    game.dropCounter += deltaTime;
    
    if (game.dropCounter > game.dropInterval) {
        game.playerDrop();
    }
    
    renderer.draw(game.arena, game.player);
    
    // تحديث النقاط مع تأثير بصري عند التغيير
    if (game.player.score !== lastScore) {
        scoreElement.innerText = game.player.score;
        scoreElement.style.animation = 'none';
        scoreElement.offsetHeight; // Trigger reflow
        scoreElement.style.animation = 'pulseScore 0.3s ease';
        lastScore = game.player.score;
    }
    
    animationId = requestAnimationFrame(update);
}

// مستمعي أحداث لوحة المفاتيح مع منع التمرير الافتراضي للأزرار الاستراتيجية
document.addEventListener('keydown', event => {
    if (['ArrowUp', 'ArrowDown', 'ArrowLeft', 'ArrowRight', ' '].includes(event.key)) {
        event.preventDefault();
    }

    if (event.key === 'ArrowLeft') {
        game.playerMove(-1);
    } else if (event.key === 'ArrowRight') {
        game.playerMove(1);
    } else if (event.key === 'ArrowDown') {
        game.playerDrop();
    } else if (event.key === 'ArrowUp') {
        game.playerRotate();
    }
});

// بدء التحدي مع إعادة الضبط السلس
startBtn.addEventListener('click', () => {
    game.reset();
    game.lastTime = performance.now();
    lastScore = 0;
    scoreElement.innerText = '0';
    
    if (animationId) {
        cancelAnimationFrame(animationId);
    }
    update();
    showToast('✨ انطلقت لعبة Block Drop Pro بنجاح!');
    startBtn.blur();
});

// تبديل الثيم الفوري (Light / Dark Theme Toggle) مع تأثير الانتقال السينمائي
themeToggle.addEventListener('click', () => {
    const htmlEl = document.documentElement;
    
    // إضافة فئة المؤثرات الانتقالية المؤقتة للجسم لضمان سلاسة التبديل
    document.body.style.pointerEvents = 'none';
    
    if (htmlEl.classList.contains('dark')) {
        htmlEl.classList.remove('dark');
        showToast('☀️ تم تفعيل الثيم النهاري الفاخر');
    } else {
        htmlEl.classList.add('dark');
        showToast('🌙 تم تفعيل الثيم الداكن الفاخر');
    }
    
    setTimeout(() => {
        document.body.style.pointerEvents = 'auto';
    }, 400);
});

// تفعيل الأسئلة الشائعة (FAQ Accordion مع دعم الإغلاق التلقائي والانسيابية)
document.querySelectorAll('.faq-question').forEach(button => {
    button.addEventListener('click', () => {
        const item = button.parentElement;
        const isActive = item.classList.contains('active');
        
        document.querySelectorAll('.faq-item').forEach(faq => faq.classList.remove('active'));
        if (!isActive) {
            item.classList.add('active');
        }
    });
});

// رسم اللوحة مبدئياً عند التحميل الأول
renderer.draw(game.arena, game.player);