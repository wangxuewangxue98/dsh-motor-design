const fs = require('fs');
const { createCanvas } = require('canvas');
const { parseSVG } = require('svg2canvas');

async function convert() {
  const svgContent = fs.readFileSync('C:/Users/15389/WorkBuddy/Worktrees/MotorDesign/main-b6c70f11/motor-ai-l0/assets/icons/icon-512.svg', 'utf8');
  
  const canvas = createCanvas(512, 512);
  const ctx = canvas.getContext('2d');
  
  // Simple gradient background
  const gradient = ctx.createLinearGradient(0, 0, 512, 512);
  gradient.addColorStop(0, '#1565C0');
  gradient.addColorStop(1, '#0D47A1');
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 512, 512);
  
  // Draw motor cross-section
  ctx.strokeStyle = '#FFFFFF';
  ctx.lineWidth = 24;
  ctx.beginPath();
  ctx.arc(256, 256, 140, 0, Math.PI * 2);
  ctx.stroke();
  
  // Inner ring
  ctx.strokeStyle = '#10B981';
  ctx.lineWidth = 16;
  ctx.beginPath();
  ctx.arc(256, 256, 80, 0, Math.PI * 2);
  ctx.stroke();
  
  // Shaft
  ctx.fillStyle = '#10B981';
  ctx.beginPath();
  ctx.arc(256, 256, 25, 0, Math.PI * 2);
  ctx.fill();
  
  // Text
  ctx.fillStyle = '#FFFFFF';
  ctx.font = 'bold 28px Arial';
  ctx.textAlign = 'center';
  ctx.fillText('MOTOR-AI L0', 256, 480);
  
  // Save
  const buffer = canvas.toBuffer('image/png');
  fs.writeFileSync('C:/Users/15389/WorkBuddy/Worktrees/MotorDesign/main-b6c70f11/motor-ai-l0/assets/icons/icon-512.png', buffer);
  console.log('Icon saved:', buffer.length, 'bytes');
}

convert().catch(console.error);
