import matplotlib.pyplot as plt
import torch
import soundfile as sf
from torch.cuda.amp import autocast
from tqdm import tqdm

from audio_zen.acoustics.feature import drop_band
from audio_zen.trainer.base_trainer import BaseTrainer
from audio_zen.acoustics.mask import build_complex_ideal_ratio_mask, decompress_cIRM

import sys
sys.path.append('../../../TAPLoss')

from TAPLoss import AcousticLoss


plt.switch_backend('agg')


class Trainer(BaseTrainer):
    def __init__(self, dist, rank, config, resume, only_validation, model, loss_function, optimizer, train_dataloader, validation_dataloader):
        super().__init__(dist, rank, config, resume, only_validation, model, loss_function, optimizer)
        self.train_dataloader = train_dataloader
        self.valid_dataloader = validation_dataloader
        self.config = config
        if self.config["acoustic_loss"]["ac_loss_weight"] != 0:
            self.ac_loss_weight = self.config["acoustic_loss"]["ac_loss_weight"]
            self.ac_loss = AcousticLoss(loss_type = self.config["acoustic_loss"]["type"], acoustic_model_path = self.config["acoustic_loss"]["model_path"], \
                           paap = self.config["acoustic_loss"]["paap"], paap_weight_path = self.config["acoustic_loss"]["paap_weight_path"]         
                           ).to(torch.device("cuda"))
    def _train_epoch(self, epoch):
        loss_total = 0.0
        ac_loss_total = 0.0
        i = 0
        for noisy, clean in tqdm(self.train_dataloader, desc="Training") if self.rank == 0 else self.train_dataloader:
            i+=1
            self.optimizer.zero_grad()

            noisy = noisy.to(self.rank)
            clean = clean.to(self.rank)

            noisy_mag, noisy_phase, noisy_real, noisy_imag = self.torch_stft(noisy)
            _, _, clean_real, clean_imag = self.torch_stft(clean)
            cIRM = build_complex_ideal_ratio_mask(noisy_real, noisy_imag, clean_real, clean_imag)  # [B, F, T, 2]
            
            cIRM = drop_band(
                cIRM.permute(0, 3, 1, 2),  # [B, 2, F ,T]
                self.model.module.num_groups_in_drop_band
            ).permute(0, 2, 3, 1)

            with autocast(enabled=self.use_amp):
                # [B, F, T] => [B, 1, F, T] => model => [B, 2, F, T] => [B, F, T, 2]
                noisy_mag = noisy_mag.unsqueeze(1)
                cRM = self.model(noisy_mag)
                cRM = cRM.permute(0, 2, 3, 1)
                enhan_loss = self.loss_function(cIRM, cRM)
                
                if self.config["acoustic_loss"]["ac_loss_only"] and self.config["acoustic_loss"]["ac_loss_weight"] <= 0:
                    raise ValueError('Weight of acoustic loss must be greater than 0 while ac_loss_only is true')


                if self.config["acoustic_loss"]["ac_loss_weight"] != 0:
                    cRM = decompress_cIRM(cRM)
                    enhanced_real = cRM[..., 0] * noisy_real - cRM[..., 1] * noisy_imag
                    enhanced_imag = cRM[..., 1] * noisy_real + cRM[..., 0] * noisy_imag                  
                    enhanced = self.torch_istft((enhanced_real, enhanced_imag), length=noisy.size(-1), input_type="real_imag")
                    ac_loss = self.ac_loss(clean, enhanced, mode = "train")
                    if self.config["acoustic_loss"]["ac_loss_only"]:
                        loss = self.ac_loss_weight * ac_loss
                    else:
                        loss = enhan_loss + self.ac_loss_weight * ac_loss
                    
                else:
                    loss = enhan_loss
                    ac_loss = torch.tensor(0)
 
                
            self.scaler.scale(loss).backward()
            self.scaler.unscale_(self.optimizer)
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.clip_grad_norm_value)
            self.scaler.step(self.optimizer)
            self.scaler.update()

            loss_total += loss.item()
            ac_loss_total += ac_loss.item()

        if self.rank == 0:
            self.writer.add_scalar(f"Loss/Train", loss_total / len(self.train_dataloader), epoch)
            self.writer.add_scalar(f"Acoustic Loss/Train", ac_loss_total / len(self.train_dataloader), epoch)

    @torch.no_grad()
    def _validation_epoch(self, epoch):
        visualization_n_samples = self.visualization_config["n_samples"]
        visualization_num_workers = self.visualization_config["num_workers"]
        visualization_metrics = self.visualization_config["metrics"]

        loss_total = 0.0
        ac_loss_total = 0.0
        loss_list = {"With_reverb": 0.0, "No_reverb": 0.0, }
        ac_loss_list = {"With_reverb": 0.0, "No_reverb": 0.0, }
        item_idx_list = {"With_reverb": 0, "No_reverb": 0, }
        noisy_y_list = {"With_reverb": [], "No_reverb": [], }
        clean_y_list = {"With_reverb": [], "No_reverb": [], }
        enhanced_y_list = {"With_reverb": [], "No_reverb": [], }
        validation_score_list = {"With_reverb": 0.0, "No_reverb": 0.0}

        # speech_type in ("with_reverb", "no_reverb")
        for i, (noisy, clean, name, speech_type) in tqdm(enumerate(self.valid_dataloader), desc="Validation"):
            assert len(name) == 1, "The batch size for the validation stage must be one."
            name = name[0]
            speech_type = speech_type[0]

            noisy = noisy.to(self.rank)
            clean = clean.to(self.rank)

            noisy_mag, noisy_phase, noisy_real, noisy_imag = self.torch_stft(noisy)
            _, _, clean_real, clean_imag = self.torch_stft(clean)
            cIRM = build_complex_ideal_ratio_mask(noisy_real, noisy_imag, clean_real, clean_imag)  # [B, F, T, 2]

            noisy_mag = noisy_mag.unsqueeze(1)
            cRM = self.model(noisy_mag)
            cRM = cRM.permute(0, 2, 3, 1)

            enhan_loss = self.loss_function(cIRM, cRM)
            
            if self.config["acoustic_loss"]["ac_loss_only"]:
                loss = 0
            else:
                loss = enhan_loss
            
            
            cRM = decompress_cIRM(cRM)

            enhanced_real = cRM[..., 0] * noisy_real - cRM[..., 1] * noisy_imag
            enhanced_imag = cRM[..., 1] * noisy_real + cRM[..., 0] * noisy_imag
            enhanced = self.torch_istft((enhanced_real, enhanced_imag), length=noisy.size(-1), input_type="real_imag")
            
            "Start of modification"
            if self.config["acoustic_loss"]["ac_loss_weight"] != 0:
                #clean_spec   = torch.cat((clean_real, clean_imag), 1)
                #enhanced_spec = torch.cat((enhanced_real, enhanced_imag), 1)
                
                #clean_spec   = clean_spec.permute(0, 2, 1)
                #enhanced_spec = enhanced_spec.permute(0, 2, 1)
                
                #ac_loss = self.ac_loss(clean_spec, enhanced_spec, mode = "eval")
                ac_loss = self.ac_loss(clean, enhanced, mode = "eval")
                loss += self.ac_loss_weight * ac_loss
            else:
                ac_loss = 0
            "End of modification"
            
            noisy = noisy.detach().squeeze(0).cpu().numpy()
            clean = clean.detach().squeeze(0).cpu().numpy()
            enhanced = enhanced.detach().squeeze(0).cpu().numpy()

            assert len(noisy) == len(clean) == len(enhanced)
            loss_total += loss
            ac_loss_total += ac_loss

            # Separated loss
            loss_list[speech_type] += loss
            ac_loss_list[speech_type] += ac_loss
            item_idx_list[speech_type] += 1

            if item_idx_list[speech_type] <= visualization_n_samples:
                self.spec_audio_visualization(noisy, enhanced, clean, name, epoch, mark=speech_type)

            noisy_y_list[speech_type].append(noisy)
            clean_y_list[speech_type].append(clean)
            enhanced_y_list[speech_type].append(enhanced)

        self.writer.add_scalar(f"Loss/Validation_Total", loss_total / len(self.valid_dataloader), epoch)
        self.writer.add_scalar(f"Acoustic Loss/Validation", ac_loss_total / len(self.valid_dataloader), epoch)

        self.writer.add_scalar(f"Loss/Validation_no_reverb", loss_list["No_reverb"] / (len(self.valid_dataloader) / 2), epoch)
        self.writer.add_scalar(f"Acoustic Loss/Validation_no_reverb", ac_loss_list["No_reverb"] / (len(self.valid_dataloader) / 2), epoch)
        
        self.writer.add_scalar(f"Loss/Validation_with_reverb", loss_list["With_reverb"] / (len(self.valid_dataloader) / 2), epoch)
        self.writer.add_scalar(f"Acoustic Loss/Validation_with_reverb", ac_loss_list["With_reverb"] / (len(self.valid_dataloader) / 2), epoch)
        for speech_type in ("With_reverb", "No_reverb"):
            self.writer.add_scalar(f"Loss/{speech_type}", loss_list[speech_type] / len(self.valid_dataloader), epoch)

            validation_score_list[speech_type] = self.metrics_visualization(
                noisy_y_list[speech_type], clean_y_list[speech_type], enhanced_y_list[speech_type],
                visualization_metrics, epoch, visualization_num_workers, mark=speech_type
            )

        return validation_score_list["No_reverb"]
