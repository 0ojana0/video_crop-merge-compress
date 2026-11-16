# video_crop-merge-compress
Just pick multiple types of videos > visually select unified cropping zone > re-encode merged &amp; cropped losslessly (preferring specifications of the best input).

It encodes using your 10-bit libx265 (-crf 15 -preset slow, yuv420p10le, hvc1, +faststart) settings anchored to the highest-spec input.
