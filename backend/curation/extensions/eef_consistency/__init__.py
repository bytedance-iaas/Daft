"""EEF-video consistency: an advisory module that compares the declared EEF projection with the
gripper independently located in the video (design docs/design/12-eef-video-consistency.md).

Input contract: docs/contracts/eef/ (eef-video/1.0.0 samples packed as trajectory-bundle/1.0).
The module never reads evaluation truth and never affects keep / drop / held.
"""
