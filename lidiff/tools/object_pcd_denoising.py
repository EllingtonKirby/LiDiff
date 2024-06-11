import imp
from operator import xor
import os
import json
import yaml
from lidiff.models.models_objects_full_diff import DiffusionPoints
import torch
import numpy as np
from nuscenes.utils.data_classes import LidarPointCloud
from nuscenes.utils.geometry_utils import BoxVisibility, transform_matrix, points_in_box
from nuscenes.utils.data_classes import Box, Quaternion
from nuscenes.nuscenes import NuScenes
import nuscenes.utils.splits as splits
import open3d as o3d
from tqdm import tqdm
from lidiff.utils.three_d_helpers import cartesian_to_cylindrical, cartesian_to_spherical, extract_yaw_angle, build_two_point_clouds
from lidiff.utils.metrics import ChamferDistance

# np.random.seed(42)
torch.cuda.empty_cache()
torch.backends.cudnn.deterministic = True

def find_eligible_objects():
    dataroot = '/datasets_local/nuscenes'
    nusc = NuScenes(version='v1.0-trainval', dataroot=dataroot, verbose=True)

    train_split = set(splits.train)

    found_target = False
    for sample in nusc.sample:
        scene_token = sample['scene_token']
        sample_token = sample['token']
        sample_data_lidar_token = sample['data']['LIDAR_TOP']
        scene_name = nusc.get('scene', scene_token)['name']
        if scene_name in train_split:
            continue

        lidar_data = nusc.get('sample_data', sample_data_lidar_token)
        lidar_filepath = os.path.join(dataroot, lidar_data['filename'])
        lidar_pointcloud = LidarPointCloud.from_file(lidar_filepath)
        objects = nusc.get_sample_data(sample_data_lidar_token)[1]

        for object in objects:
            if 'vehicle.car' in object.name:
                annotation = nusc.get('sample_annotation', object.token)
                num_lidar_points = annotation['num_lidar_pts']
                car_points = points_in_box(object, lidar_pointcloud.points[:3, :])
                car_info = {
                    'lidar_filepath': lidar_filepath,
                    'center': object.center.tolist(),
                    'wlh': object.wlh.tolist(),
                    'rotation_real': object.orientation.real.tolist(),
                    'rotation_imaginary': object.orientation.imaginary.tolist(),
                    'car_points': car_points.tolist(),
                    'num_lidar_points': num_lidar_points
                }
                
                if num_lidar_points > 300:
                    found_target = True
                    break
        if found_target:
            break
    return car_info

def load_pcd(pcd_file):
    if pcd_file.endswith('.bin'):
        return np.fromfile(pcd_file, dtype=np.float32).reshape((-1, 5))[:, :3]

    else:
        print(f"Point cloud format '.{pcd_file.split('.')[-1]}' not supported. (supported formats: .bin (kitti format), .ply)")

def visualize_step_t(x_t, pcd):
    points = x_t.detach().cpu().numpy()
    pcd.points = o3d.utility.Vector3dVector(points)
    return pcd

def p_sample_loop(model: DiffusionPoints, x_init, x_t, x_cond, x_uncond, batch_indices, generate_viz=False):
    model.scheduler_to_cuda()
    if generate_viz:
        viz_pcd = o3d.geometry.PointCloud()
        os.makedirs(f'lidiff/random_pcds/generated_pcd/step_visualizations', exist_ok=True)

    for t in tqdm(range(len(model.dpm_scheduler.timesteps))):
        t = model.dpm_scheduler.timesteps[t].cuda()[None]

        with torch.no_grad():
            noise_t = model.classfree_forward(x_t, x_cond, x_uncond, t).squeeze(0)
            torch.cuda.empty_cache()
        input_noise = x_t.F

        x_t = model.dpm_scheduler.step(noise_t, t, input_noise)['prev_sample']
        
        x_t = model.points_to_tensor(x_t, batch_indices)

        if generate_viz:
            viz = visualize_step_t(x_t.F.clone(), viz_pcd)
            print(f'Saving Visualization of Step {t}')
            o3d.io.write_point_cloud(f'lidiff/random_pcds/generated_pcd/step_visualizations/full_diff_cylindrical_coord_step_{t[0]}.ply', viz)

        torch.cuda.empty_cache()

    return x_t

        
def denoise_object_from_pcd(model: DiffusionPoints, lidar_filepath, car_points, center, wlh, angle, num_diff_samples):
    center = np.array(center)
    x_size = torch.Tensor(wlh).float().unsqueeze(0)
    x_orientation = torch.ones((1,1)) * angle
    pcd = load_pcd(lidar_filepath)
    x_object = torch.from_numpy(pcd[car_points]) - center
    x_center = torch.from_numpy(cartesian_to_cylindrical(center[None, :])).float()

    x_init = x_object.clone().cuda()    
    batch_indices = torch.zeros(x_init.shape[0]).long().cuda()

    x_cond = torch.cat((torch.hstack((x_center[:,0][:, None], x_orientation)), torch.hstack((x_center[:,1:], x_size))),-1).cuda()
    x_uncond = torch.zeros_like(x_cond).cuda()

    local_chamfer = ChamferDistance()
    x_gen_evals = []
    for i in tqdm(range(num_diff_samples)):
        torch.manual_seed(i)
        torch.cuda.manual_seed(i)
        x_feats = torch.randn(x_init.shape, device=model.device)
        x_t = model.points_to_tensor(x_feats, batch_indices)
        x_gen_eval = p_sample_loop(model, x_init, x_t, x_cond, x_uncond, batch_indices, generate_viz=False)
        x_gen_evals.append(x_gen_eval.F)    
        pcd_pred, pcd_gt = build_two_point_clouds(genrtd_pcd=x_gen_eval.F, object_pcd=x_init)
        local_chamfer.update(pcd_gt, pcd_pred)
        print(f"Seed {i} CD: {local_chamfer.last_cd()}")

    best_index = local_chamfer.best_index()
    print(f"Best seed: {best_index}")
    x_gen_eval = x_gen_evals[best_index]

    return x_gen_eval.cpu().detach().numpy(), x_object.cpu().detach().numpy()

def find_pcd_and_test_on_object(output_path, name):
    config = 'lidiff/config/object_generation/config_object_generation_cyclical_coords.yaml'
    weights = 'lidiff/checkpoints/nuscenes_cars_generation_cyclical_coordinates_1_epoch=99.ckpt'
    cfg = yaml.safe_load(open(config))
    model = DiffusionPoints.load_from_checkpoint(weights, hparams=cfg).cuda()
    model.eval()
    car_info = find_eligible_objects()
    rotation_real = np.array(car_info['rotation_real'])
    rotation_imaginary = np.array(car_info['rotation_imaginary'])
    orientation = Quaternion(real=rotation_real, imaginary=rotation_imaginary)

    x_gen, x_orig = denoise_object_from_pcd(
        model,
        car_info['lidar_filepath'], 
        car_info['car_points'], 
        car_info['center'], 
        car_info['wlh'], 
        extract_yaw_angle(orientation),
        1,
    )
    np.savetxt(f'{output_path}/{name}_generated.txt', x_gen)
    np.savetxt(f'{output_path}/{name}_orig.txt', x_orig)

if __name__=='__main__':
    find_pcd_and_test_on_object('lidiff/random_pcds/generated_pcd', 'full_diff_250_steps_2')
