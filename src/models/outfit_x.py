import numpy as np
import torch

from PIL import Image
from torch import nn
from .configs import OutfitXConfig
from .encoders import ItemEncoder
from .datatypes import (
    OutfitComplementaryItemRetrievalTask,
    OutfitCompatibilityPredictionTask,
    OutfitPrecomputeEmbeddingTask,
    OutfitFillInTheBlankTask
)
from typing import List, Union, Optional, Type, TypeVar


class OutfitX(nn.Module):
    Tasks = TypeVar(
        'Tasks',
        OutfitComplementaryItemRetrievalTask,
        OutfitCompatibilityPredictionTask,
        OutfitFillInTheBlankTask,
        OutfitPrecomputeEmbeddingTask,
    )
    def __init__(self, cfg: Optional[OutfitXConfig]= None):
        super().__init__()
        self.cfg = cfg if cfg is not None else OutfitXConfig()
        # 1 编码器设置
        ## 1.1 服装单品编码器
        self.item_encoder = ItemEncoder(self.cfg.item_encoder)
        ## 1.2 全局服装编码器 global outfit representation
        transformer_encoder_layer = nn.TransformerEncoderLayer(
                d_model=self.item_encoder.d_embed,
                nhead=self.cfg.transformer.n_head,
                dim_feedforward=self.cfg.transformer.d_ffn,
                dropout=self.cfg.transformer.dropout,
                batch_first=self.cfg.transformer.batch_first,
                norm_first=self.cfg.transformer.norm_first,
                activation=self.cfg.transformer.activation,
        )
        self.transformer_encoder =nn.TransformerEncoder(
            encoder_layer=transformer_encoder_layer,
            num_layers=self.cfg.transformer.n_layers,
            enable_nested_tensor=self.cfg.transformer.enable_nested_tensor,
        )
        # ## 1.3 全局任务嵌入向量(学习全局任务的特征)
        # self.task_emb = nn.Parameter(
        #     torch.randn(self.item_encoder.d_embed // 2) * 0.02, requires_grad=True
        # )

        # 2 '兼容性预测'(Compatibility Prediction,CP)
        ## 2.1 服装令牌(学习一个全局外观表示 a learnable embedding)
        self.outfit_token = nn.Parameter(
            torch.randn(self.item_encoder.d_embed) * 0.02, requires_grad=True
        )
        ## 2.2 处理CP问题的前馈网络,输出outfit的兼容性得分 MLP
        self.cp_ffn = nn.Sequential(
            nn.Dropout(self.cfg.transformer.dropout),
            nn.Linear(self.item_encoder.d_embed, 1),
            # nn.Sigmoid()
        )

        ## 3 '互补项检索'(Complementary Item Retrieval,CIR)
        ## 3.1 处理CIR问题的前馈网络,输出目标单品的嵌入向量 MLP
        self.cir_ffn = nn.Sequential(
            nn.Linear(self.item_encoder.d_embed, self.cfg.d_embed, bias=False)
        )
        ## 3.2 CIR任务的空图像嵌入向量
        self.target_item_image_emb = nn.Parameter(
            torch.randn(self.item_encoder.d_embed // 2) * 0.02, requires_grad=True
        )

        # 4 pad
        ## 4.1 用于填充文本和图片形状
        # self.image_pad = Image.new("RGB", self.item_encoder.image_size)
        # self.text_pad = ''
        # ## 4.2 用于填充outfit的嵌入向量
        # self.pad_emb = nn.Parameter(
        #     torch.randn(self.item_encoder.d_embed) * 0.02, requires_grad=False
        # )

        # 5 Others
        ## 5.1 任务字典,用于根据任务类型调用对应的forward
        self.forward_ = {
            OutfitCompatibilityPredictionTask: self._cp_forward,
            OutfitComplementaryItemRetrievalTask: self._cir_forward,
            OutfitFillInTheBlankTask: self._cir_forward,
            OutfitPrecomputeEmbeddingTask: self.precompute_embeddings,
            # FashionItem: self._get_item_embeddings
        }

    @property
    def device(self) -> torch.device:
        """Returns the device on which the model's parameters are stored."""
        return next(self.parameters()).device  # 最常用方式

    def forward(
        self,
        task: Type[Tasks],
        *args,
        **kwargs
    ):
        _forward = self.forward_[task]
        return _forward(*args, **kwargs)


    def precompute_embeddings(
        self,
        images: List[List[Union[np.ndarray, Image.Image]]],
        texts: List[List[str]],
    )->np.ndarray:
        """
        使用encoder 预计算item的嵌入向量，用于后续的训练 only train
        """
        # (B,1,d_embed)
        embeddings = self.item_encoder(images,texts)
        item_embedding_list = embeddings[:,0,:]
        return item_embedding_list

    def _cp_forward(
        self,
        outfit_embedding: torch.Tensor,
        outfit_mask: torch.Tensor,
        encoder_input_dict: Optional[dict] = None,
     )->torch.Tensor:
        if encoder_input_dict is not None:
            outfit_embedding = self.item_encoder(**encoder_input_dict)
        B, L, d_embed = outfit_embedding.shape
        transformer_inputs = torch.cat([
                self.outfit_token.unsqueeze(0).unsqueeze(0).expand(B, -1, -1), # (B,1,d_embed) d_embed=item_encoder.d_embed
                outfit_embedding # (B,L,d_embed)
             ],dim=1).contiguous() # (B,1+L,d_embed)
        mask = torch.cat([
            torch.zeros(B, 1, dtype=torch.bool, device=self.device), # [B, 1]
            outfit_mask # [B, L]
        ], dim=1).contiguous() # [B, 1+L]
        transformer_outputs = self.transformer_encoder(
            src=transformer_inputs,
            src_key_padding_mask=mask
        ).contiguous()
        # 取出outfit_token的输出
        outfit_token_states = transformer_outputs[:, 0, :].contiguous() # [B, d_embed]
        scores = self.cp_ffn(outfit_token_states) # [B, 1]
        return scores


    def _cir_forward(
        self,
        outfit_embedding: torch.Tensor, # [B, L, d_embed]
        outfit_mask: torch.Tensor, # [B, L]
        target_item_text_embedding: torch.Tensor, # [B, d_embed//2]
    )->torch.Tensor:
        B, L, d_embed = outfit_embedding.shape
        target_items_image_embedding =self.target_item_image_emb.unsqueeze(0).expand(B,-1) # [B, d_embed//2]
        target_items_embedding = torch.cat(
            [target_items_image_embedding,target_item_text_embedding],
            dim=-1
        ).unsqueeze(1)# [B,1, d_embed]

        embeddings = torch.cat([target_items_embedding, outfit_embedding],dim=1).contiguous()# [B, 1, d_embed] + [B, L, d_embed] -> [B, 1+L, d_embed]

        prefix_mask = torch.zeros(B, 1, dtype=torch.bool, device=self.device)
        mask = torch.cat([prefix_mask, outfit_mask], dim=1).contiguous() # [B, 1+L]

        transformer_outputs = self.transformer_encoder(
            src=embeddings,
            src_key_padding_mask=mask
        ).contiguous()
        # 取出target_item_token的输出
        target_item_token_states = transformer_outputs[:, 0, :].contiguous() # [B, d_embed]
        target_item_embeddings = self.cir_ffn(target_item_token_states) # [B, d_embed]
        return target_item_embeddings

#################################
# import numpy as np
# import torch
#
# from PIL import Image
# from torch import nn
# from .configs import OutfitXConfig
# from .encoders import ItemEncoder
# from .datatypes import FashionItem,OutfitComplementaryItemRetrievalTask,OutfitCompatibilityPredictionTask
# from typing import List, Union, Optional
#
#
# class OutfitX(nn.Module):
#     def __init__(self, cfg: Optional[OutfitXConfig]= None):
#         super().__init__()
#         self.cfg = cfg if cfg is not None else OutfitXConfig()
#         # 1 编码器设置
#         ## 1.1 服装单品编码器
#         self.item_encoder = ItemEncoder(self.cfg.item_encoder)
#         ## 1.2 全局服装编码器 global outfit representation
#         transformer_encoder_layer = nn.TransformerEncoderLayer(
#                 d_model=self.item_encoder.d_embed,
#                 nhead=self.cfg.transformer.n_head,
#                 dim_feedforward=self.cfg.transformer.d_ffn,
#                 dropout=self.cfg.transformer.dropout,
#                 batch_first=self.cfg.transformer.batch_first,
#                 norm_first=self.cfg.transformer.norm_first,
#                 activation=self.cfg.transformer.activation,
#         )
#         self.transformer_encoder =nn.TransformerEncoder(
#             encoder_layer=transformer_encoder_layer,
#             num_layers=self.cfg.transformer.n_layers,
#             enable_nested_tensor=self.cfg.transformer.enable_nested_tensor,
#         )
#         # ## 1.3 全局任务嵌入向量(学习全局任务的特征)
#         # self.task_emb = nn.Parameter(
#         #     torch.randn(self.item_encoder.d_embed // 2) * 0.02, requires_grad=True
#         # )
#
#         # 2 '兼容性预测'(Compatibility Prediction,CP)
#         ## 2.1 服装令牌(学习一个全局外观表示 a learnable embedding)
#         self.outfit_token = nn.Parameter(
#             torch.randn(self.item_encoder.d_embed) * 0.02, requires_grad=True
#         )
#         ## 2.2 处理CP问题的前馈网络,输出outfit的兼容性得分 MLP
#         self.cp_ffn = nn.Sequential(
#             nn.Dropout(self.cfg.transformer.dropout),
#             nn.Linear(self.item_encoder.d_embed, 1),
#             # nn.Sigmoid()
#         )
#
#         ## 3 '互补项检索'(Complementary Item Retrieval,CIR)
#         ## 3.1 处理CIR问题的前馈网络,输出目标单品的嵌入向量 MLP
#         self.cir_ffn = nn.Sequential(
#             nn.Linear(self.item_encoder.d_embed, self.cfg.d_embed, bias=False)
#         )
#         ## 3.2 CIR任务的空图像嵌入向量
#         self.target_item_image_emb = nn.Parameter(
#             torch.randn(self.item_encoder.d_embed // 2) * 0.02, requires_grad=True
#         )
#
#         # 4 pad
#         ## 4.1 用于填充文本和图片形状
#         self.image_pad = Image.new("RGB", self.item_encoder.image_size)
#         self.text_pad = ''
#         ## 4.2 用于填充outfit的嵌入向量
#         self.pad_emb = nn.Parameter(
#             torch.randn(self.item_encoder.d_embed) * 0.02, requires_grad=True
#         )
#
#         # 5 Others
#         ## 5.1 任务字典,用于根据任务类型调用对应的forward
#         self.task_ = {
#             OutfitCompatibilityPredictionTask: self._cp_forward,
#             OutfitComplementaryItemRetrievalTask: self._cir_forward,
#             # FashionItem: self._get_item_embeddings
#         }
#
#     @property
#     def device(self) -> torch.device:
#         """Returns the device on which the model's parameters are stored."""
#         return next(self.parameters()).device  # 最常用方式
#
#     def forward(self,
#         queries: List[Union[OutfitCompatibilityPredictionTask, OutfitComplementaryItemRetrievalTask, FashionItem]],
#         *args, **kwargs
#     ):
#         _type = type(queries[0])
#         _forward = self.task_[_type]
#         return _forward(queries, *args, **kwargs)
#
#
#     def precompute_embeddings(self,items: List[FashionItem])->np.ndarray:
#         """
#         使用encoder 预计算item的嵌入向量，用于后续的训练 only train
#         :param items: item list
#         :return: item_embedding_list：np.ndarray（B,d_embed）
#         """
#         # (B,1)
#         items = [[item] for item in items]
#         # (B,1,d_embed)
#         embeddings,_ = self._get_embeddings_and_padding_masks(items)
#         item_embedding_list = embeddings[:,0,:].cpu().detach().numpy()
#         return item_embedding_list
#
#     def _cp_forward(self,
#         cp_queries: List[OutfitCompatibilityPredictionTask],
#         use_precomputed_embedding: bool = True
#      )->torch.Tensor:
#         queries = [query.outfit for query in cp_queries]
#         embeddings,mask = self._get_embeddings_and_padding_masks(queries, use_precomputed_embedding)
#         B, L, d_embed = embeddings.shape
#         transformer_inputs = torch.cat([
#                 self.outfit_token.unsqueeze(0).unsqueeze(0).expand(B, -1, -1), # (B,1,d_embed) d_embed=item_encoder.d_embed
#                 embeddings # (B,L,d_embed)
#              ],dim=1) # (B,1+L,d_embed)
#         mask = torch.cat([
#             torch.zeros(B, 1, dtype=torch.bool, device=self.device), # [B, 1]
#             mask # [B, L]
#         ], dim=1) # [B, 1+L]
#         transformer_outputs = self.transformer_encoder(
#             src=transformer_inputs,
#             src_key_padding_mask=mask
#         )
#         # 取出outfit_token的输出
#         outfit_token_states = transformer_outputs[:, 0, :] # [B, d_embed]
#         scores = self.cp_ffn(outfit_token_states)
#         return scores
#
#     def _cir_forward(self,
#         cir_queries: List[OutfitComplementaryItemRetrievalTask],
#         use_precomputed_embedding: bool = True
#     )->torch.Tensor:
#         outfits = [query.outfit for query in cir_queries]
#         # [B,1+L,d_embed]
#         embeddings,mask = self._get_embeddings_and_padding_masks(outfits, use_precomputed_embedding)
#         B, L, d_embed = embeddings.shape
#         target_items_text_embedding = torch.tensor( # [B, d_embed//2]
#             [query.target_item.text_embedding for query in cir_queries],
#             dtype=torch.float32,
#             device=self.device
#         )
#         target_items_image_embedding =self.target_item_image_emb.unsqueeze(0).expand(B,-1) # [B, d_embed//2]
#         target_items_embedding = torch.cat(
#             [target_items_image_embedding,target_items_text_embedding],
#             dim=-1
#         ).unsqueeze(1)# [B,1, d_embed]
#         embeddings = torch.cat([target_items_embedding, embeddings],dim=1)# [B, 1, d_embed] + [B, L, d_embed] -> [B, 1+L, d_embed]
#
#         prefix_mask = torch.zeros(B, 1, dtype=torch.bool, device=self.device)
#         mask = torch.cat([prefix_mask, mask], dim=1) # [B, 1+L]
#
#         transformer_outputs = self.transformer_encoder(
#             src=embeddings,
#             src_key_padding_mask=mask
#         )
#         # 取出target_item_token的输出
#         target_item_token_states = transformer_outputs[:, 0, :] # [B, d_embed]
#         target_item_embeddings = self.cir_ffn(target_item_token_states) # [B, d_embed]
#         return target_item_embeddings
#
#     def _get_item_embeddings(self,
#             items: List[FashionItem],
#             use_precomputed_embedding: bool = False
#         )->torch.Tensor:
#         """
#         训练完后被使用,用于构建目标item的嵌入向量，该嵌入用于计算相似度（FAISS）
#         :param items:
#         :param use_precomputed_embedding:
#         :return:输出单品的embedding，用于和cir任务输出的目标单品embedding进行相似度查询，即knn(cir_embedding)≈knn(item_embedding)
#         """
#         # (B,1)
#         items = [[item] for item in items]
#         # (B,1,d_embed)
#         embeddings,mask = self._get_embeddings_and_padding_masks(items, use_precomputed_embedding)
#         transformer_outputs = self.transformer_encoder(
#             src=embeddings,
#             src_key_padding_mask=mask
#         )
#         item_token_states = transformer_outputs[:, 0, :] # [B, d_embed]
#         item_embeddings = self.cir_ffn(item_token_states) # [B, d_embed]
#         return item_embeddings
#
#     def _get_embeddings_and_padding_masks(
#         self,
#         sequences: List[List[FashionItem]],
#         use_precomputed_embedding: bool = False,
#     ):
#         max_length = self._get_max_length(sequences)
#
#         if use_precomputed_embedding:
#             embeddings = self._pad_sequences(
#                 sequences=[[item.embedding for item in outfit] for outfit in sequences],
#                 max_length=max_length,
#                 pad_value=self.pad_emb,
#                 return_tensor=True
#             )
#         else:
#             # 对outfit进行填充
#             images = self._pad_sequences(
#                 sequences=[[item.image for item in outfit] for outfit in sequences],
#                 max_length=max_length,
#                 pad_value=self.image_pad
#             )
#             texts = self._pad_sequences(
#                 sequences=[[f"{item.category}"for item in outfit] for outfit in sequences],
#                 max_length=max_length,
#                 pad_value=self.text_pad
#             )
#             # item_encoder需要接收的是长度一样的outfit序列,所以需要对outfit进行填充
#             embeddings = self.item_encoder(images, texts)
#         item_length = lambda seq: min(len(seq), max_length)
#         pad_length = lambda seq: max_length - item_length(seq)
#         mask = torch.tensor(
#             data=[
#                 [0] * item_length(outfit) + [1] * (pad_length(outfit))
#                 for outfit in sequences
#             ],
#             dtype=torch.bool,
#             device=self.device
#         )
#         embeddings = embeddings.contiguous()
#         mask = mask.contiguous()
#         return embeddings,mask
#
#     def _get_max_length(self, sequences):
#         """
#         max_length的length指的是每个outfit中的item数量
#         :param sequences:序列
#         :return:每个outfit中的item数量的最大值,即max_length
#         """
#         if self.cfg.padding == 'max_length':
#             return self.cfg.max_length
#         max_length = max(len(seq) for seq in sequences)
#
#         return min(self.cfg.max_length, max_length) if self.cfg.truncation else max_length
#
#     def _pad_sequences(self, sequences, pad_value, max_length,return_tensor=False):
#         # if self.cfg.truncation OR self.cfg.padding == 'max_length'：
#         # len(seq)可能大于max_length，需要对序列进行截断
#         item_length = lambda seq: min(len(seq), max_length)
#         pad_length = lambda seq: max_length - item_length(seq)
#         if return_tensor:
#             return torch.stack([
#                 torch.cat(
#                     tensors=[
#                         torch.tensor(
#                             data=np.array(seq)[:item_length(seq)],
#                             dtype=torch.float,
#                             device=self.device
#                         ),
#                         pad_value.expand(pad_length(seq), -1)
#                     ],
#                     dim=0
#                 )
#                 for seq in sequences
#             ]) # 堆叠后的张量默认在子元素所在设备：self.device
#         else:
#             return [
#                 seq[:item_length(seq)] + [pad_value] * (pad_length(seq))
#                 for seq in sequences
#             ]
