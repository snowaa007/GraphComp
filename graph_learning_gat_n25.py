from skimage.segmentation import felzenszwalb
from skimage import graph
from skimage.util import img_as_float
import numpy as np
import networkx as nx
import matplotlib.pyplot as plt

import os
import time
import pickle
import torch
import torch.nn as nn
import torch.optim as optim
import torch.nn.functional as F
from torch_geometric.data import Data
from torch_geometric.nn import MessagePassing
from torch_geometric.nn import GCNConv, BatchNorm, GATConv
from torch_geometric.nn import global_mean_pool
from torch_geometric.utils import to_networkx


def graph_similarity(input_graph, decoded_graph):
    mse_loss = F.mse_loss(decoded_graph, input_graph)
    return mse_loss.item()

def graph_initialization(temperature_matrix):
    # Assuming temperature_matrix is your original data matrix
    temperature_matrix = img_as_float(temperature_matrix)

    # Use felzenszwalb method for segmentation
    segments_fz = felzenszwalb(temperature_matrix, scale=500, sigma=1, min_size=100)

    # Create a Region Adjacency Graph using mean temperatures
    rag = graph.rag_mean_color(temperature_matrix, segments_fz)

    # Iterate over regions
    for region in np.unique(segments_fz):
        # Find the mean temperature of the region
        region_pixels = temperature_matrix[segments_fz == region]
        mean_temperature = np.mean(region_pixels)
        
        # Assign the mean temperature to the corresponding node in the graph
        rag.nodes[region]['mean temperature'] = mean_temperature

    # Define edge weights as absolute difference of mean temperatures
    for edge in rag.edges:
        source, target = edge
        diff = np.abs(rag.nodes[source]['mean temperature'] - rag.nodes[target]['mean temperature'])

        # Set edge weight to 0 if the temperature difference is below a certain threshold, otherwise set it to 1
        weight = 0 if diff <= 0 else 1
        
        rag.edges[source, target]['weight'] = weight

        return rag

def calculate_mean_std(train_graphs):
    all_temperatures = [data['mean temperature'] for graph in train_graphs for _, data in graph.nodes(data=True)]
    mean_temp = np.mean(all_temperatures)
    std_temp = np.std(all_temperatures)
    return mean_temp, std_temp

def normalize_temperature(train_graphs, mean_temp, std_temp):
    normalized_graphs = []
    for graph in train_graphs:
        normalized_graph = graph.copy()
        for node, data in normalized_graph.nodes(data=True):
            data['mean temperature'] = (data['mean temperature'] - mean_temp) / std_temp
        normalized_graphs.append(normalized_graph)
    return normalized_graphs

def denormalize_temperature(train_graphs, mean_temp, std_temp):
    for graph in train_graphs:
        for node, data in graph.nodes(data=True):
            data['mean temperature'] = (data['mean temperature'] * std_temp) + mean_temp

# # 定义无监督的图卷积神经网络模型
class UnsupervisedGNNModel(MessagePassing):
    def __init__(self, input_size, hidden_size, output_size):
        super(UnsupervisedGNNModel, self).__init__(aggr='mean')
        self.conv1 = GATConv(input_size, hidden_size, heads=8, concat=True)
        self.bn1 = BatchNorm(hidden_size * 8)
        self.conv2 = GATConv(hidden_size * 8, output_size, heads=8, concat=False)

    def forward(self, x, edge_index, edge_attr=None):
        x = self.conv1(x, edge_index)
        x = self.bn1(x)
        x = F.relu(x)
        x = self.conv2(x, edge_index)
        return x

class AutoEncoder(torch.nn.Module):
    def __init__(self, input_size, hidden_size, output_size):
        super(AutoEncoder, self).__init__()
        self.encoder = UnsupervisedGNNModel(input_size, hidden_size, output_size)
        self.decoder = UnsupervisedGNNModel(output_size, hidden_size, input_size)

    def forward(self, x, edge_index, edge_attr=None):
        encoded = self.encoder(x, edge_index, edge_attr)
        decoded = self.decoder(encoded, edge_index, edge_attr)
        return decoded
    
def mse_loss(decoded, original):
    return ((decoded - original) ** 2).mean()

def train(model, graphs, criterion, optimizer, device, epochs):
    model.train()

    for epoch in range(epochs):
        total_loss = 0  

        for graph_data in graphs:
            x = torch.tensor([graph_data.nodes[node]['mean temperature'] for node in graph_data.nodes], dtype=torch.float32).view(-1, 1)
            edge_index = torch.tensor([[edge[0] for edge in graph_data.edges], [edge[1] for edge in graph_data.edges]], dtype=torch.long)
            edge_attr = torch.tensor([graph_data.edges[edge]['weight'] for edge in graph_data.edges], dtype=torch.float32).view(-1, 1)

            data = Data(x=x, edge_index=edge_index, edge_attr=edge_attr)
            data = data.to(device)

            optimizer.zero_grad()
            decoded = model(data.x, data.edge_index, data.edge_attr)
            loss = criterion(decoded, data.x)
            loss.backward()
            optimizer.step()

            total_loss += loss.item()

        print(f'Epoch {epoch + 1}: Loss: {total_loss / len(graphs)}')

    return model

def reconstruct_graphs(autoencoder, graphs, device):
    reconstructed_graphs = []
    autoencoder.eval()
    with torch.no_grad():
        for graph_data in graphs:
            x = torch.tensor([graph_data.nodes[node]['mean temperature'] for node in graph_data.nodes], dtype=torch.float32).view(-1, 1)
            edge_index = torch.tensor([[edge[0] for edge in graph_data.edges], [edge[1] for edge in graph_data.edges]], dtype=torch.long)
            data = Data(x=x, edge_index=edge_index).to(device)

            encoded = autoencoder.encoder(data.x, data.edge_index, data.edge_attr)
            decoded = autoencoder.decoder(encoded, data.edge_index, data.edge_attr)

            reconstructed_graph = to_networkx(Data(x=decoded, edge_index=data.edge_index), to_undirected=True)
            # 为每个节点设置 'mean temperature'
            for node_id, mean_temp in enumerate(decoded):
                reconstructed_graph.nodes[node_id]['mean temperature'] = mean_temp.item()

            reconstructed_graphs.append(reconstructed_graph)

    return reconstructed_graphs

def compare_graphs(original_rags, reconstructed_graphs):
    similarities = []
    for i, (original, reconstructed) in enumerate(zip(original_rags, reconstructed_graphs)):
        # 比较节点数量
        original_nodes = len(original.nodes())
        reconstructed_nodes = len(reconstructed.nodes())
        nodes_difference = abs(original_nodes - reconstructed_nodes)

        # 比较边数量
        original_edges = len(original.edges())
        reconstructed_edges = len(reconstructed.edges())
        edges_difference = abs(original_edges - reconstructed_edges)

        # 计算节点特征的相似性（如果适用）
        if original_nodes == reconstructed_nodes:
            original_features = np.array([original.nodes[node]['mean temperature'] for node in original.nodes()])
            reconstructed_features = np.array([data['mean temperature'] for _, data in reconstructed.nodes(data=True)])
            feature_diff = original_features - reconstructed_features
            feature_mae = np.mean(np.abs(feature_diff))
            # feature_mse = np.mean(feature_diff ** 2)
            feature_std = np.std(feature_diff)
        else:
            feature_mae = np.nan  # 无法比较不同数量节点的特征
            feature_std = np.nan

        similarities.append({
            'graph_index': i + 1,
            'node_count_difference': nodes_difference,
            'edge_count_difference': edges_difference,
            'feature_mae': feature_mae,
            'feature_std': feature_std
        })

    return similarities

def compare_edge_consistency(original_rags, reconstructed_graphs):
    consistency_results = []
    for i, (original, reconstructed) in enumerate(zip(original_rags, reconstructed_graphs)):
        # 将 RAG 对象和 NetworkX 图的边转换为集合形式
        original_edges = set(original.edges())
        reconstructed_edges = set(reconstructed.edges())

        # 检查边的一致性
        consistent_edges = original_edges.intersection(reconstructed_edges)
        inconsistent_edges_original = original_edges.difference(reconstructed_edges)
        inconsistent_edges_reconstructed = reconstructed_edges.difference(original_edges)

        # 计算一致性比例
        consistency_ratio = len(consistent_edges) / (len(original_edges) + len(reconstructed_edges) - len(consistent_edges))

        consistency_results.append({
            'graph_index': i + 1,
            'consistent_edges': len(consistent_edges),
            'inconsistent_edges_original': len(inconsistent_edges_original),
            'inconsistent_edges_reconstructed': len(inconsistent_edges_reconstructed),
            'consistency_ratio': consistency_ratio
        })

    return consistency_results

def view_graphs_without_fig(graphs, reconstructed_graphs):
    selected_indices = [0, 1, 2]  # 选择前三个图（根据需要更改索引）

    for i in selected_indices:
        original_graph = graphs[i]
        reconstructed_graph = reconstructed_graphs[i]

        print(f"Graph {i + 1} Node Temperatures:")
        for node in original_graph.nodes():
            original_temperature = original_graph.nodes[node]['mean temperature']
            reconstructed_temperature = reconstructed_graph.nodes[node]['mean temperature']
            temperature_difference = abs(original_temperature - reconstructed_temperature)
            print(f"Node {node}: Original Temperature = {original_temperature}, Reconstructed Temperature = {reconstructed_temperature}, Temperature Difference = {temperature_difference}")

        print("\n")

def view_graphs(graphs, reconstructed_graphs):
    threshold = 0.88
    selected_indices = [0, 1, 2,3,4,5]  # 选择前三个图（根据需要更改索引）
    temperature_differences = []
    differences_above_threshold = 0

    for i in range(len(graphs)):
        original_graph = graphs[i]
        reconstructed_graph = reconstructed_graphs[i]

        # print(f"Graph {i + 1} Node Temperatures:")
        for node in original_graph.nodes():
            original_temperature = original_graph.nodes[node]['mean temperature']
            reconstructed_temperature = reconstructed_graph.nodes[node]['mean temperature']
            temperature_difference = abs(original_temperature - reconstructed_temperature)
            temperature_differences.append(temperature_difference)
            # print(f"Node {node}: Original Temperature = {original_temperature}, Reconstructed Temperature = {reconstructed_temperature}, Temperature Difference = {temperature_difference}")

            if temperature_difference > threshold:
                differences_above_threshold += 1

        print("\n")

    # 统计超过阈值的温度差异比例
    proportion_above_threshold = differences_above_threshold / len(temperature_differences)
    # 打印超过阈值的比例
    print(f"Proportion of temperature differences above {threshold}: {proportion_above_threshold:.2f}")

    # 绘制温度差异的分布图
    plt.figure(figsize=(10, 6))
    plt.hist(temperature_differences, bins=100, color='blue', alpha=0.7)
    plt.title("Distribution of Temperature Differences")
    plt.xlabel("Temperature Difference")
    plt.ylabel("Frequency")
    plt.savefig("Distribution of Temperature Differences.png")

def main():

    graphs_file_path = 'graphs-mini-size-1k.pkl'
    model_path = 'auto_gat_2l_o32_1k_e1000.pth'
    # model_path = 'auto_gat_3l_o32_e100.pth'

    # 检查是否存在保存的图数据文件
    if os.path.exists(graphs_file_path):
        # 如果文件存在，直接加载图数据
        with open(graphs_file_path, 'rb') as file:
            graphs = pickle.load(file)
        print("load graphs done.")
    else:
        # 如果文件不存在，生成图数据
        temperature_data = np.fromfile('/home/lig0d/compression/sample_t2.dat', dtype=np.float32).reshape(num_timepoints, wide, length)
        print("temperature_data.shape: ", temperature_data.shape)

        # temperature_data = np.round(temperature_data, 2)

        # 对每个输入数据应用 graph_initialization 函数
        graphs = [graph_initialization(data_matrix) for data_matrix in temperature_data]
        print("graph_initialization done.")

        # 保存图数据到本地
        with open(graphs_file_path, 'wb') as file:
            pickle.dump(graphs, file)
    
    mean_temp, std_temp = calculate_mean_std(graphs)
    normalized_graphs = normalize_temperature(graphs, mean_temp, std_temp)

    # 初始化无监督的图卷积神经网络模型
    input_size = 1  # 假设每个节点的特征是一个实数
    hidden_size = 32
    output_size = 32

    if os.path.exists(model_path):
        # 如果已经存在模型文件，直接加载模型
        unsupervised_gnn_model = AutoEncoder(input_size, hidden_size, output_size).to(device)
        unsupervised_gnn_model.load_state_dict(torch.load(model_path))
        unsupervised_gnn_model.eval()
    else:
        # 否则，训练模型并保存
        unsupervised_gnn_model = AutoEncoder(input_size, hidden_size, output_size).to(device)
        # 定义损失函数和优化器
        criterion = nn.MSELoss()
        optimizer = optim.Adam(unsupervised_gnn_model.parameters(), lr=0.01)

        start_time = time.time()
        print("training start...")
        representations = train(unsupervised_gnn_model, normalized_graphs, criterion, optimizer, device, epochs)
        torch.save(unsupervised_gnn_model.state_dict(), model_path)

        end_time = time.time()
        total_time = end_time - start_time
        print(f'Training done. Total time: {total_time:.2f} seconds')

    reconstructed_graphs = reconstruct_graphs(unsupervised_gnn_model, normalized_graphs, device)
    denormalize_temperature(reconstructed_graphs, mean_temp, std_temp)
    # consistency_results = compare_edge_consistency(graphs, reconstructed_graphs)
    view_graphs(graphs, reconstructed_graphs)

    # 比较原始图和重构图
    # similarities = compare_graphs(graphs, reconstructed_graphs)
    # for item in consistency_results:
    #     print(item)
    #     print()  # 添加一个换行符
    
    # 示例：评估自编码器
    # avg_mse = evaluate_model(unsupervised_gnn_model, graphs, device)
    # print(f'Average MSE: {avg_mse}')
    # for feature in structural_features:
        # print(feature)

    # 打印前5个图的表示
    # print('len of representations: ', len(representations))
    
    # for i, graph_representation in enumerate(representations[-1]):
    #     print(f'Final Representation for Graph {i + 1}:\n{graph_representation}')


    # # 假设 'rag' 是你的图对象 
    # for i in range(len(graphs)):
    #     num_nodes = graphs[i].number_of_nodes()
    #     num_edges = graphs[i].number_of_edges()
    #     print("Number of nodes and edges: ", num_nodes, num_edges) 
    #     #print("Number of edges : ", num_edges)


if __name__ == '__main__':

    total_size = 1038825 #1215
    num_timepoints = 500
    wide = 855
    length  = 1215
    input_channel = 2
    epochs  = 100
    loss_type = "mse"
    model_version = 1
    os.environ["CUDA_VISIBLE_DEVICES"] = "1"
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    main()


