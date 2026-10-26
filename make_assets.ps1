Add-Type -AssemblyName System.Drawing
$TaskRoot = $PSScriptRoot
$TaskGreen = [System.Drawing.ColorTranslator]::FromHtml('#173f35')
$TaskLime = [System.Drawing.ColorTranslator]::FromHtml('#d9edb5')
$TaskInk = [System.Drawing.ColorTranslator]::FromHtml('#243c34')
$TaskGray = [System.Drawing.ColorTranslator]::FromHtml('#6d7b74')
$TaskIcon = New-Object System.Drawing.Bitmap 64,64
$TaskGraphics = [System.Drawing.Graphics]::FromImage($TaskIcon)
$TaskGraphics.SmoothingMode = 'AntiAlias'
$TaskGraphics.Clear($TaskGreen)
$TaskPen = New-Object System.Drawing.Pen $TaskLime,4
$TaskGraphics.DrawRectangle($TaskPen,16,18,32,25)
$TaskGraphics.DrawLine($TaskPen,23,12,41,12)
$TaskBrush = New-Object System.Drawing.SolidBrush $TaskLime
$TaskGraphics.FillEllipse($TaskBrush,18,42,10,10)
$TaskGraphics.FillEllipse($TaskBrush,36,42,10,10)
$TaskGraphics.DrawLine($TaskPen,24,28,28,28)
$TaskGraphics.DrawLine($TaskPen,36,28,40,28)
$TaskPng = New-Object System.IO.MemoryStream
$TaskIcon.Save($TaskPng,[System.Drawing.Imaging.ImageFormat]::Png)
$TaskPayload = $TaskPng.ToArray()
$TaskOutput = [System.IO.File]::Create((Join-Path $TaskRoot 'app.ico'))
$TaskWriter = New-Object System.IO.BinaryWriter $TaskOutput
$TaskWriter.Write([UInt16]0); $TaskWriter.Write([UInt16]1); $TaskWriter.Write([UInt16]1)
$TaskWriter.Write([byte]64); $TaskWriter.Write([byte]64); $TaskWriter.Write([byte]0); $TaskWriter.Write([byte]0)
$TaskWriter.Write([UInt16]1); $TaskWriter.Write([UInt16]32); $TaskWriter.Write([UInt32]$TaskPayload.Length); $TaskWriter.Write([UInt32]22)
$TaskWriter.Write($TaskPayload); $TaskWriter.Dispose(); $TaskOutput.Dispose()
$TaskPng.Dispose(); $TaskGraphics.Dispose(); $TaskIcon.Dispose(); $TaskPen.Dispose(); $TaskBrush.Dispose()

$TaskCanvas = New-Object System.Drawing.Bitmap 1000,620
$TaskDraw = [System.Drawing.Graphics]::FromImage($TaskCanvas)
$TaskDraw.SmoothingMode = 'AntiAlias'
$TaskDraw.TextRenderingHint = 'AntiAliasGridFit'
$TaskDraw.Clear([System.Drawing.ColorTranslator]::FromHtml('#f3f5ef'))
$TaskFontTitle = New-Object System.Drawing.Font 'Microsoft YaHei',22
$TaskFont = New-Object System.Drawing.Font 'Microsoft YaHei',12
$TaskSmall = New-Object System.Drawing.Font 'Microsoft YaHei',10
$TaskInkBrush = New-Object System.Drawing.SolidBrush $TaskInk
$TaskGrayBrush = New-Object System.Drawing.SolidBrush $TaskGray
$TaskGreenBrush = New-Object System.Drawing.SolidBrush $TaskGreen
$TaskWhiteBrush = New-Object System.Drawing.SolidBrush ([System.Drawing.Color]::White)
$TaskDraw.DrawString('门店安装位置示意', $TaskFontTitle, $TaskInkBrush,40,28)
$TaskDraw.DrawString('演示附件 · 虚构场地，仅用于查看资料归档效果', $TaskFont, $TaskGrayBrush,42,77)
$TaskDraw.FillRectangle($TaskWhiteBrush,40,128,920,415)
$TaskBorder = New-Object System.Drawing.Pen ([System.Drawing.ColorTranslator]::FromHtml('#cbd5cb')),2
$TaskDraw.DrawRectangle($TaskBorder,40,128,920,415)
$TaskRoute = New-Object System.Drawing.Pen ([System.Drawing.ColorTranslator]::FromHtml('#4e9a75')),7
$TaskDraw.DrawRectangle($TaskRoute,188,225,615,215)
$TaskDraw.FillRectangle($TaskGreenBrush,75,355,160,65)
$TaskDraw.DrawString('取餐 / 归位区', $TaskFont, $TaskWhiteBrush,89,376)
$TaskDraw.FillRectangle($TaskGreenBrush,748,150,170,52)
$TaskDraw.DrawString('路由器安装位', $TaskFont, $TaskWhiteBrush,770,165)
foreach ($TaskTable in @(@(295,170,'01桌'),@(475,170,'02桌'),@(650,170,'03桌'),@(295,462,'04桌'),@(475,462,'05桌'),@(650,462,'06桌'))) {
    $TaskDraw.FillRectangle((New-Object System.Drawing.SolidBrush ([System.Drawing.ColorTranslator]::FromHtml('#e7eddf'))),[int]$TaskTable[0],[int]$TaskTable[1],95,46)
    $TaskDraw.DrawString([string]$TaskTable[2],$TaskFont,$TaskInkBrush,([int]$TaskTable[0]+20),([int]$TaskTable[1]+11))
}
foreach ($TaskNode in @(@(188,225,'1'),@(495,225,'2'),@(803,225,'3'),@(803,440,'4'),@(495,440,'5'),@(188,440,'6'))) {
    $TaskDraw.FillEllipse($TaskWhiteBrush,([int]$TaskNode[0]-16),([int]$TaskNode[1]-16),32,32)
    $TaskDraw.DrawEllipse($TaskBorder,([int]$TaskNode[0]-16),([int]$TaskNode[1]-16),32,32)
    $TaskDraw.DrawString([string]$TaskNode[2],$TaskSmall,$TaskInkBrush,([int]$TaskNode[0]-5),([int]$TaskNode[1]-9))
}
$TaskDraw.DrawString('绿色线：轨道示意     圆点：定位卡     安装资料项可重新上传替换', $TaskFont, $TaskGrayBrush,42,565)
$TaskCanvas.Save((Join-Path $TaskRoot 'static\demo-installation.png'),[System.Drawing.Imaging.ImageFormat]::Png)
$TaskDraw.Dispose(); $TaskCanvas.Dispose()
$TaskFontTitle.Dispose(); $TaskFont.Dispose(); $TaskSmall.Dispose(); $TaskInkBrush.Dispose(); $TaskGrayBrush.Dispose(); $TaskGreenBrush.Dispose(); $TaskWhiteBrush.Dispose(); $TaskBorder.Dispose(); $TaskRoute.Dispose()
