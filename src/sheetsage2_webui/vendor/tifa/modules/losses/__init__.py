from .hmm_loss import HMMForwardLossWithEmissions, CTCLossWithoutBlank
from .region_loss import FrameAlignmentLoss, SpanContrastiveLoss
from .spec_loss import SpectrogramReconstructionLoss
from .token_loss import TokenIdentityLoss, FrameIdentityLoss
